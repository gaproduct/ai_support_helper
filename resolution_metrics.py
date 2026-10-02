"""
Метрики времени обработки тикета из истории диалога.

Считает по одному диалогу (список сообщений) набор метрик, которые мы
обсуждали и проверяли на живых обращениях:

  • first_response       — время до первого СОДЕРЖАТЕЛЬНОГО ответа поддержки
                           (заглушки «мы получили сообщение / ожидайте» не считаются);
  • resolution           — время до фактического решения. Решение фиксируется по
                           явному маркеру («выполнено»/подтверждение клиента) ИЛИ
                           ролевым признаком: последнее содержательное сообщение —
                           ответ поддержки (не вопрос/не запрос данных) либо
                           подтверждение закрытия клиентом. Системные авто-логи
                           кошелька за сообщения не считаются;
  • handoff              — сколько вопрос провёл в смежных отделах
                           (комплаенс / финансовый / документооборот / банк-провайдер),
                           отдельно по каждому отделу и суммарно.

Дополнительно:
  • сегментация переписки на отдельные ИНЦИДЕНТЫ (по паузе между сообщениями),
    чтобы не склеивать в один тикет разные обращения одного клиента;
  • дедупликация повторяющихся сообщений (Flomni иногда отдаёт дубли);
  • календарное и «рабочее» (в рамках графика поддержки) время;
  • статус инцидента: resolved / pending_department / awaiting_support / open.

Модуль не зависит от источника: на вход — список нормализованных сообщений
вида {"text", "direction": "inbound|outbound", "time": ISO8601}. Есть тонкий
адаптер для таблицы dialogs и CLI для ручного прогона.

CLI:
  docker exec -i support_tickets-scheduler-1 python resolution_metrics.py --dialog-id 3244
  docker exec -i support_tickets-scheduler-1 python resolution_metrics.py --client-id <hash>
  python resolution_metrics.py --file messages.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация распознавания текста
# ─────────────────────────────────────────────────────────────────────────────

MSK = timezone(timedelta(hours=3))

# График работы поддержки для «рабочего» времени (в часовом поясе MSK).
# Окно задано руководителем поддержки: смены идут с 08:00 до 21:00 все дни
# недели. Прежнее 10:00-19:00 Пн-Пт отсекало 19% сообщений операторов и
# обнуляло время решения у 19% инцидентов.
WORK_TZ = MSK
WORK_START = time(8, 0)    # 08:00
WORK_END = time(21, 0)     # 21:00
WORK_DAYS = {0, 1, 2, 3, 4, 5, 6}  # все дни недели (0 = понедельник)

# Порог паузы, после которой переписка считается новым инцидентом.
INCIDENT_GAP_HOURS = 24

# Заглушки бывают двух видов:
#  1) HOLD_TEMPLATES — целиковые бот/шаблон-автоответы и «холды» (взяли, ждите).
#     Достаточно совпадения по фразе — всё сообщение считается шумовым.
#  2) GREETING_NOISE — приветствия/вежливость. Считаются шумом только если после
#     их вырезания в сообщении не осталось содержательного текста (residue-логика).
# Сообщение, попавшее в RESOLUTION_DONE, заглушкой НЕ считается (приоритет решения).
HOLD_TEMPLATES = [
    r"мы получили ваше сообщение",
    r"скоро ответим",
    r"свяжемся с вами",
    r"на связи компания madetask",
    r"сервис по работе и выплатам удал",
    r"вам ответит первый освободившийся оператор",
    r"в ожидании ответа вы можете",
    r"дождитесь.{0,20}подключени.{0,15}оператор",
    r"уточняем информацию по выплате",
    r"уточняем информацию по запросу",
    r"проверяем информацию по вашему вопросу",
    r"проверяем информацию.{0,30}ожидайте",
    r"уточняем информацию.{0,30}ожидайте",
    r"выберите интересующий",
    r"мы не получили от вас оценк",
    r"окончательно закрыли диалог",
    r"ваш запрос принят",
    r"ответ будет в ближайшее время",
    r"здесь можно получить ответы по работе в madetask",
    r"как зарегистрироваться.{0,40}работать с задачами",
    # англоязычные холды
    r"still working on your request",
    r"we will let you know as soon",
    r"thank you for your patience",
    r"we are (still )?working on",
    # «нужно ещё время» — обещание, не ответ
    r"продолжаем работу над вашим запросом",
    r"потребуется.{0,20}больше времени",
    r"нужно немного больше времени",
    # обещание вернуться с ответом — тоже холд, не ответ. Иначе диалог,
    # оборвавшийся на «проверим и ответим», считался решённым (ролевое 4b).
    # Ручная проверка руководителя: SG-022, SG-035.
    r"ответим в ближайшее время",
    r"верн(е|ё)мся с ответом",
    r"проверим детали",
    # «ваш вопрос у нас на рассмотрении», «выплата в процессе проверки» —
    # холды. Иначе ролевое 4b закрывало тикет таким сообщением
    # (руками: SG-108, SG-114, SG-156).
    r"вопрос.{0,20}на рассмотрении",
    r"в процессе проверки",
    # ручная проверка по всем операторам (1-20 сентября): вариации холдов
    # (руками: SG-009, AB-003, AB-005, MG-010)
    r"взяли ваш запрос в работу",
    r"запрос зафиксирован",
    r"запрос.{0,25}находится в работе",
    r"мониторим (заявку|запрос)",
    r"точно не подскаж",
    # англоязычные «передали команде/коллегам» — передача или холд, не решение
    # (руками: AT-002/AZ-001, AT-005, AZ-008, FE-001)
    r"(forwarded|passed)[^.!?]{0,45}(team|colleagues|specialists|department)",
    r"your request has been forwarded",
    r"we[’']?ve (started|begun) working on your request",
]
GREETING_NOISE = [
    r"здравствуйте",
    r"добрый день",
    r"доброе утро",
    r"добрый вечер",
    r"добрый вечер",
    r"спасибо за обращение",
    r"^\W*\?+\W*$",
    r"^\W*(спасибо|благодарю|спс|ок|окей|понятно|принято)\W*$",
]

# Автозакрытие / опрос качества — игнорируем полностью.
CSAT_PATTERNS = [
    r"оцените.{0,20}(качество|работу|оператора)",
    r"насколько.{0,20}довольны",
    r"ваш диалог (закрыт|завершен)",
    r"диалог будет закрыт",
    r"поставьте оценку",
]

# Открытие передачи в смежный отдел → department.
HANDOFF_OPEN = {
    "compliance": [
        # английские передачи («contacted our Compliance team» — руками: MG, 5201)
        r"compliance (team|department)",
        r"переда(ли|ем|н).{0,30}комплаенс",
        # «направили запрос в отдел комплаенс» — та же передача, другой глагол
        # (руками: SG-036, SG-037)
        r"(направ|отправ)(или|им|ляем|или)?\w*.{0,30}комплаенс",
        # голое «на рассмотрении» — обычный холд («ваш вопрос у нас на
        # рассмотрении»), а не передача в отдел (руками: SG-055, SG-085, SG-088).
        # Передачей считается только явное «передали на рассмотрение».
        r"переда(ли|ем|н).{0,25}на рассмотрение",
        r"служб.{0,15}безопасност",
        r"отдел.{0,15}проверк",
    ],
    "finance": [
        r"financ(e|ial) (team|department)",
        r"переда(ли|ем|н).{0,30}(финанс|финотдел|казначейств)",
        r"уточня(ем|ю).{0,20}(у )?финанс",
        r"финансов.{0,10}отдел",
        r"(коллеги из|у) бухгалтери",
        r"уточня(ют|ем|ю).{0,20}бухгалтер",
        r"направили запрос коллегам.{0,40}(дат|задач|принят)",
    ],
    "documents": [
        # «shared the information with our document management department»
        # (руками: MG, 7649)
        r"document management (team|department)",
        r"переда(ли|ем|н).{0,30}документооборот",
        r"(направ|отправ)\w*.{0,30}документооборот",
        r"отдел.{0,15}документооборот",
    ],
    # «Юридическое лицо» встречается в поддержке постоянно и к отделу отношения
    # не имеет, поэтому одно «юрид» тут не маркер. Ловим либо отдел явно, либо
    # юристов вместе с глаголом обращения.
    "legal": [
        r"переда(ли|ем|н|ла|л).{0,30}юрид",
        r"юридическ.{0,10}отдел",
        r"юр\.?\s?отдел",
        r"(уточн|запрос|спрос|согласу|согласова|проверя)\w*.{0,25}(у|от) юрист",
        r"ожида\w*.{0,20}(ответ|информац)\w*.{0,15}от юрист",
        r"на согласовании у юрист",
        r"юристы (запросили|предлагают|проверя|смотр|рассматрива|подготов)",
    ],
    "bank_provider": [
        r"banking (team|department)",
        r"запрос.{0,20}(в )?банк",
        r"уточня(ем|ю).{0,20}(у )?(банк|провайдер|платежн)",
        r"направили.{0,20}провайдер",
        r"на стороне (банка|провайдера)",
    ],
    "technical": [
        r"technical (team|department)",
        r"development team",
        # «We have submitted a request to return the funds» — запрос в смежный
        # отдел на возврат/отмену (руками: MG, 7650)
        r"submitted a request to (return|cancel)",
        r"переда(ли|ем|н).{0,30}(техническ|тех\.?\s?отдел|разработ)",
        r"техническ.{0,10}отдел",
        r"переда(ли|ем|н).{0,30}для отмены задачи",
    ],
}

# Закрытие передачи (ответ пришёл из смежного отдела / вопрос вернулся).
HANDOFF_CLOSE = [
    r"коллеги (подтвердили|проверили)",
    r"получил(и)? ответ",
    r"по ответу.{0,15}отдел",
    r"реквизит(ы)?.{0,20}(подтвержден|разблокирован)",
    r"проверка завершена",
    r"вопрос вернул",
    r"согласован.{0,20}(с )?(финансов|финотдел|бухгалтер)",
    r"перенос.{0,25}согласован",
    # смежный отдел вернулся с ответом («коллеги сообщили, что…») — передача
    # закрыта; является ли это резолвом решает ролевой признак (гард на вопрос).
    r"коллеги (сообщили|ответили|вернулись|уточнили)",
    r"обновили.{0,25}(фио|данные|информаци|в его личном|в личном кабинет)",
    # «Благодарим за ожидание» — скриптовая фраза, с которой поддержка
    # возвращается к клиенту после паузы. Сама по себе она встречается и в
    # ответах без передачи, но здесь проверяется только когда передача открыта,
    # так что контекст задан. От случая «ещё ждём» защищает HANDOFF_STILL_WAITING.
    r"(благодарим|спасибо)[^.!?]{0,15}за (длительное )?ожидание",
    r"коллеги.{0,25}(передали|проверили|обнаружили|подсказали)",
    r"согласно информации, полученной от",
    r"направляем.{0,25}(готовый )?(документ|платежн)",
    r"(провайдер|банк).{0,20}(сообщил|передал|ответил|подтвердил)",
]

# Ответ пришёл, но он про то, что ответа ещё нет. Гасит маркеры закрытия:
# «Благодарим за ожидание! Запрос находится в работе, сообщим позже» — это
# не возврат из отдела, а вторая просьба подождать, передача остаётся открытой.
HANDOFF_STILL_WAITING = [
    r"ожида(ем|ет|ется).{0,40}(ответ|информаци|решени)",
    r"как только (получим|будет|появится)",
    r"(находятся|находится|остаётся|остается) в работе",
    r"(работают|работает) над",
    r"сразу (сообщим|проинформируем|напишем|свяжемся)",
    r"ожидайте",
    r"уточня(ем|ю)",
]

# Фактическое РЕШЕНИЕ со стороны поддержки (выполнено), а не обещание.
RESOLUTION_DONE = [
    r"реквизит(ы)?.{0,20}(подтвержден|разблокирован)",
    r"можете повторить выплату",
    r"выплат\w*.{0,25}(прош|заверш|отправл|выполн|проведен|обработан|успешн)",
    r"документ(ы)?.{0,20}(отправлен|готов|направлен)",
    r"пробле(ма|му).{0,20}(решен|устранен)",
    r"вопрос(?:\s+\w+){0,3}\s+решен|вопрос(?:\s+\w+){0,3}\s+решён",
    r"рады.{0,25}(решен|решён)",
    r"успешно (завершен|выполнен|проведен)",
    r"доступ.{0,20}восстановлен",
    r"зачислен(ы|о)?.{0,20}баланс|баланс пополнен",
    r"баланс.{0,25}зачислен",
    r"выплата.{0,15}(была )?направлена",
    r"(были )?рады.{0,10}(вам )?помочь",
    r"(средства|деньги|сумма).{0,20}вернул",
    r"(средства|деньги|сумма).{0,25}(успешно\s+)?(отправлен|перечислен|переведен|зачислен)",
    r"вернул.{0,20}(на )?баланс",
    r"задача.{0,15}отменен",
    r"(исполнитель|сотрудник).{0,30}верифицирован",
    r"верификаци.{0,20}(проведена|успешн|завершена)",
    r"успешно верифицирован",
    r"реквизит(ы)?.{0,20}(разблокирован|подтвержден)",
    r"(второй|дублирующ\w*|лишн\w*)\s+аккаунт.{0,15}удал",
    r"согласован.{0,20}(с )?(финансов|финотдел|бухгалтер)",
    r"перенос.{0,25}(задач).{0,20}согласован",
    r"перенос.{0,25}согласован.{0,20}(финансов|финотдел|бухгалтер)",
    # Определённый ответ + внешний редирект (причина на стороне банка → в банк).
    r"обратитесь.{0,20}банк",
    r"на стороне банка",
    r"отклонен.{0,50}(политик|ограничени).{0,15}(банк|счёт|счет)",
    r"отклонен.{0,50}(банк|счёт|счет).{0,20}(обратитесь|на стороне)",
    # Смена налогового статуса выполнена (ИП / физлицо / самозанятый).
    r"налогов\w*\s+статус.{0,30}(изменен|изменён|поменя|смен)",
    r"(изменил|поменял|сменил)\w*.{0,20}налогов\w*\s+статус",
    r"статус.{0,15}профил\w*.{0,25}(изменен|изменён)",
    # англоязычные маркеры решения (поддержка)
    r"successfully processed",
    r"has been (successfully )?(processed|completed|sent|resolved)",
    # «being processed» — процесс, не результат, поэтому голое processed не берём
    # (руками: FE-001); завершённое «has been processed» ловит шаблон выше
    r"(payment|payout).{0,25}(completed|successful)\b",
    # «payout has been processed successfully» — завершено; голое «being
    # processed» остаётся холдом (руками: FE-001, MG 5201)
    r"(payment|payout).{0,30}processed successfully",
    r"is now (completed|resolved|done)",
    r"has been verified",
]

# Подтверждение решения СО СТОРОНЫ КЛИЕНТА («всё пришло», «платёж дошёл»).
# Простое «спасибо» сюда НЕ входит — это не сигнал решения.
CLIENT_RESOLUTION = [
    r"платеж.{0,15}(дошел|дошёл|пришел|пришёл|поступил|прошел|прошёл)",
    r"деньги.{0,15}(пришли|поступили|дошли)",
    r"выплата.{0,15}(пришла|поступила|дошла)",
    r"вопрос.{0,15}(решил|решён|решен)",
    r"уже.{0,10}решил",
    r"всё работает|все работает",
    r"всё пришло|все пришло",
    r"получил(а)?\s+выплату",
    # Закрытие консультации: клиент всё понял и решил НЕ выполнять действие
    # («понял, спасибо, тогда можно не менять»). Голое «спасибо/понял» — не в счёт.
    r"(понял|поняла|ясно|понятно).{0,40}(не мен|не буд|не над|не нуж|можно не)",
    r"можно не мен",
    r"тогда не буд(у|ем)",
    # клиент сам подтвердил успех; фиксируем решение, даже если поддержка
    # после этого собирает диагностику (руками: AT-001, AZ-002)
    r"(всё|все)\s+получилось",
    r"(всё|все)\s+вывел",
]

# Обещания / взяли в работу — это НЕ решение (ETA), фиксируем отдельно.
PROMISE_PATTERNS = [
    r"взяли в работу",
    r"переда(ли|ем)",
    r"рассмотрим",
    r"вернемся с ответом|вернёмся с ответом",
    r"в течение.{0,20}(час|дн|рабоч)",
]

# Системные авто-фиды (лог транзакций кошелька), приходящие как inbound —
# это НЕ сообщения клиента и НЕ тикеты. Помечаем как шум, чтобы они не
# становились ни точкой старта, ни first_response, ни (ложным) resolution.
SYSTEM_FEED_PATTERNS = [
    r"обработка транзакции завершена",
    r"проверяем исходящую транзакцию",
    r"вывод средств на адрес",
    r"usdt отправлена на адрес",
    r"комиссия:\s*[\d.,'\s]+usdt",
    # авто-уведомления кошелька/депозита (прилетают как inbound, не тикеты)
    r"депозит получен",
    r"сумма:\s*[\d.,'\s]+usdt",
    r"ваша.{0,15}транзакц.{0,15}(была )?отклонена",
    r"^\s*made\s*task\s*:",
]

# Роль последнего сообщения поддержки. Если это ЗАПРОС данных/уточняющий вопрос —
# тикет реально ждёт клиента (не resolved). Если утверждение/ответ — resolved.
SUPPORT_ASK_PATTERNS = [
    r"\?",
    r"подскажите", r"укажите", r"уточните", r"пришлите", r"напишите",
    r"отправьте", r"направьте", r"предоставьте", r"нужно отправить",
    r"прошу.{0,20}(прислать|направить|уточнить)",
    r"чем.{0,10}помочь", r"какой", r"какую", r"когда вам удобно",
    # англоязычные запросы данных (руками: FE-001)
    r"please (provide|send|share|specify|confirm|clarify|attach|upload)",
    r"could you (please\s+)?(provide|send|share|specify|confirm|clarify|tell)",
    r"we await the requested information",
]

# Закрывающий вопрос вежливости в конце ответа («остались ли ещё вопросы?»,
# «могу ли ещё чем-то помочь?»). Это НЕ запрос данных: если клиент промолчал,
# тикет решён ответом выше, а не «ждёт клиента». Ручная разметка руководителя
# считает решением содержательный ответ перед таким вопросом
# (SG-060, SG-065, SG-083, SG-105, SG-115).
CLOSING_ASK_PATTERNS = [
    # опциональный лид «Подскажите,» / «пожалуйста» срезаем вместе с вопросом,
    # иначе после среза оставался бы голый «подскажите» и ловился как запрос
    # данных (руками: SG-105)
    r"(?:(?:под)?скажите[,!\s]+)?(?:пожалуйста[,!\s]+)?остал(ись|ся|ось)\s*(ли)?[^.!?\n]{0,25}вопрос\w*\s*\??",
    r"(?:(?:под)?скажите[,!\s]+)?(?:пожалуйста[,!\s]+)?могу\s+(ли\s+я\s+|я\s+|ли\s+)?[^.!?\n]{0,20}(ещё|еще)[^.!?\n]{0,20}помочь\s*\??",
    r"(чем|что)[- ]?то\s+(ещё|еще)\s+помочь\s*\??",
    r"(ещё|еще)\s+чем[- ]?(то|нибудь)?\s*(могу\s+)?помочь\s*\??",
]

# Авто-сообщения бота при входе в чат (приветствие, «дождитесь оператора»,
# закрытие без оценки). Первым ответом поддержки НЕ считаются. Операторская
# заглушка «мы получили ваше сообщение» — считается: её отправляет живой
# оператор, и руководитель поддержки в ручной разметке берёт именно её.
BOT_AUTO_PATTERNS = [
    r"на связи компания madetask",
    r"здесь можно получить ответы по работе в madetask",
    r"как зарегистрироваться.{0,40}работать с задачами",
    r"дождитесь.{0,20}подключени.{0,15}оператор",
    r"вам ответит первый освободившийся оператор",
    r"в ожидании ответа вы можете",
    r"выберите интересующий",
    r"мы не получили от вас оценк",
    r"окончательно закрыли диалог",
]

# Клиент подтвердил закрытие своим последним сообщением → resolved.
CLIENT_CLOSE_PATTERNS = [
    r"спасибо", r"благодар", r"увид", r"получил", r"дошло",
    r"разобрал", r"всё понятно|все понятно",
    # клиент сам снял вопрос (узко — только про сам вопрос/проблему)
    r"вопрос.{0,10}не актуал", r"уже не актуал", r"больше не актуал",
    r"вопрос (снят|отпал)", r"снимаю вопрос", r"уже (не нужно|не надо)",
    r"ладно.{0,12}разбер", r"сам(и|а)? разбер", r"вс[её] ок\b",
    r"хорошего (дня|вечера)",
    # англоязычные закрытия
    r"thank you", r"thanks", r"\bthx\b", r"appreciate",
    r"have a (nice|good|lovely|great)",
    r"\balright\b", r"all right", r"sounds good", r"got it", r"noted",
]

# Парный признак «проблема ушла»: поддержка спрашивает, актуален ли ещё вопрос…
ACTUALITY_Q_PATTERNS = [
    r"актуальн",
    r"не\s+актуал",
    r"остал[иася]{1,3}.{0,6}вопрос",
    r"вопрос.{0,10}актуал",
    r"проблема.{0,15}(сохран|актуальн|остаётся|остается)",
    r"(ещё|еще).{0,8}актуал",
    r"(всё|все).{0,5}получилось",
]
# …а клиент отвечает отрицанием/закрытием (без «не работает» — то ловит _NEG_RESOLUTION).
CLIENT_NEG_CLOSE_PATTERNS = [
    r"нет,?\s*конечно",
    r"\bуже нет\b",
    r"не актуал",
    r"всё ок|все ок",
    r"всё хорошо|все хорошо",
    r"уже реш",
    r"больше не (беспоко|актуал|нужн)",
    r"сам[а]? разобрал",
]

# Клиент подтвердил, что принял ОТВЕТ поддержки (не благодарность, а «понятно/окей»).
# Считается закрытием только если предыдущий ход поддержки был ответом, а не
# вопросом/холдом (гард в compute_incident).
CLIENT_ACK_PATTERNS = [
    r"понятно",
    r"понял[аи]?\b",
    r"\bхорошо\b",
    r"\bясно\b",
    r"окей", r"\bок\b", r"\bok\b", r"\bokay\b",
    r"без проблем",
    r"договорились",
    r"принял[аи]?\b",
    r"передал[аи]? (ему|ей|им|информаци)",
]


# Клиент ПОДТВЕРДИЛ резюме/ответ поддержки («все так», «верно», «именно так»).
# Считается закрытием только если предыдущий ход поддержки — реальный ответ либо
# проверка актуальности (гард в compute_incident).
CLIENT_CONFIRM_PATTERNS = [
    r"\bвсё так\b", r"\bвсе так\b", r"\bтак и есть\b", r"\bименно так\b",
    r"\bсовершенно верно\b", r"\bвсё верно\b", r"\bвсе верно\b",
    r"\bвсё правильно\b", r"\bвсе правильно\b", r"\bда,?\s*верно\b",
    r"\bверно\b",
]

# Хвостовой клиентский филлер: «подожду», «буду ждать» — не требует ответа
# поддержки и не должен ни ломать close, ни держать тикет в awaiting_support.
CLIENT_FILLER_PATTERNS = [
    r"^\W*подожд", r"^\W*буду ждать", r"^\W*ладно,?\s*подожд",
    r"^\W*ок(ей)?,?\s*подожд", r"^\W*хорошо,?\s*подожд",
]


def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Отрицание перед глаголом решения: «выплата НЕ прошла», «деньги НЕ пришли»,
# «вопрос НЕ решён» — такие сообщения решением НЕ являются.
_NEG_RESOLUTION = re.compile(r"\bне\s+(прош|приш|дош|поступ|реш|работ|выполн|заверш|отправл|восстанов|получ)", re.IGNORECASE)

_HOLD = _compile(HOLD_TEMPLATES)
_GREETING = _compile(GREETING_NOISE)
_CSAT = _compile(CSAT_PATTERNS)
_HANDOFF_OPEN = {dep: _compile(pats) for dep, pats in HANDOFF_OPEN.items()}
_HANDOFF_CLOSE = _compile(HANDOFF_CLOSE)
_HANDOFF_STILL_WAITING = _compile(HANDOFF_STILL_WAITING)
_RESOLUTION = _compile(RESOLUTION_DONE)
_CLIENT_RESOLUTION = _compile(CLIENT_RESOLUTION)
_PROMISE = _compile(PROMISE_PATTERNS)
_SYSTEM_FEED = _compile(SYSTEM_FEED_PATTERNS)
_SUPPORT_ASK = _compile(SUPPORT_ASK_PATTERNS)
_CLOSING_ASK = _compile(CLOSING_ASK_PATTERNS)
_BOT_AUTO = _compile(BOT_AUTO_PATTERNS)
_CLIENT_CLOSE = _compile(CLIENT_CLOSE_PATTERNS)
_CLIENT_CONFIRM = _compile(CLIENT_CONFIRM_PATTERNS)
_CLIENT_FILLER = _compile(CLIENT_FILLER_PATTERNS)
_ACTUALITY_Q = _compile(ACTUALITY_Q_PATTERNS)
_CLIENT_NEG_CLOSE = _compile(CLIENT_NEG_CLOSE_PATTERNS)
_CLIENT_ACK = _compile(CLIENT_ACK_PATTERNS)
# Плейсхолдеры вложений (Telegram/chatapp) — нечитаемый контент.
_ATTACHMENT_RE = re.compile(
    r"\[(image|file|photo|video|audio|voice|sticker|документ|фото|видео|"
    r"голосовое|аудио|стикер)[^\]]*\]",
    re.IGNORECASE,
)
# Ссылки на внутренние инструменты (Slack, Notion) — служебные заметки
# операторов друг другу, а не ответ клиенту (руками: MG-010).
_INTERNAL_LINK_RE = re.compile(
    r"https?://\S*(?:slack\.com|notion\.(?:com|so|site))\S*", re.IGNORECASE
)
_LEADING_AUTHOR = re.compile(r"^\s*@\S+[^:]*:\s*")

# В групповых чатах (messenger) и клиент, и сотрудник поддержки/CSM приходят как
# inbound с префиксом "@handle Имя:". Отличить поддержку можно только по хендлу.
# _csm — Customer Success Manager. SUPPORT_GROUP_HANDLES — явный настраиваемый
# ростер аккаунт-менеджеров/CSM, которых нужно считать поддержкой в группе.
SUPPORT_GROUP_HANDLES: set[str] = {
    # Клиентские менеджеры MadeTask — личные Telegram-аккаунты, пишут в группы
    # как inbound. Явный ростер (подтверждён вручную), регистр/@ нормализованы.
    "viciousmane",
    "ragrarr",
    "cerber650",
    "elen_csm",
    "vkudelevich",
    "cathieremozo",
    "gaprod1",
}

# chatapp/Telegram-подключённые чаты: все сообщения приходят inbound, роль зашита
# в поле author_id (не в тексте). Ростер внутренних сотрудников по author_id —
# подтверждён вручную.
SUPPORT_AGENT_IDS: set[str] = {
    "7692309574",   # RemozoSupport (основной саппорт-аккаунт)
    "470130440",    # Oksana
    "5110608275",   # Andrei S.
    "7289441632",   # Cathie at remozo.com
    "7955379769",   # Елена
}
_SUPPORT_HANDLE_RE = re.compile(r"^\s*@(\S+)", re.IGNORECASE)
_SUPPORT_HANDLE_SUFFIX = re.compile(r"_csm$", re.IGNORECASE)


def _group_author_is_support(text: str) -> bool:
    """True, если inbound-сообщение группы отправлено сотрудником поддержки/CSM
    (по хендлу: суффикс _csm либо явный ростер SUPPORT_GROUP_HANDLES)."""
    m = _SUPPORT_HANDLE_RE.match(text or "")
    if not m:
        return False
    handle = m.group(1).rstrip(":").lower()
    return handle in SUPPORT_GROUP_HANDLES or bool(_SUPPORT_HANDLE_SUFFIX.search(handle))


# ─────────────────────────────────────────────────────────────────────────────
# Модель
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Msg:
    ts: datetime
    direction: str            # inbound | outbound
    text: str
    is_stub: bool = False
    is_csat: bool = False

    @property
    def is_client(self) -> bool:
        return self.direction == "inbound"

    @property
    def is_support(self) -> bool:
        return self.direction == "outbound"

    @property
    def substantive(self) -> bool:
        """Содержательное сообщение (не заглушка, не опрос, есть текст)."""
        return bool(self.text.strip()) and not self.is_stub and not self.is_csat


@dataclass
class Handoff:
    department: str
    opened_at: datetime
    closed_at: Optional[datetime] = None

    @property
    def seconds(self) -> Optional[float]:
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


@dataclass
class TicketMetrics:
    incident_index: int
    started_at: Optional[datetime] = None
    first_response_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    first_response_seconds: Optional[float] = None
    resolution_seconds: Optional[float] = None
    resolution_working_seconds: Optional[float] = None
    handoffs: list[Handoff] = field(default_factory=list)
    handoff_seconds_by_dept: dict[str, float] = field(default_factory=dict)
    handoff_seconds_total: float = 0.0
    status: str = "open"
    n_messages: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("started_at", "first_response_at", "resolved_at"):
            d[k] = self.__dict__[k].isoformat() if self.__dict__[k] else None
        d["handoffs"] = [
            {
                "department": h.department,
                "opened_at": h.opened_at.isoformat(),
                "closed_at": h.closed_at.isoformat() if h.closed_at else None,
                "seconds": h.seconds,
            }
            for h in self.handoffs
        ]
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Нормализация входа
# ─────────────────────────────────────────────────────────────────────────────

def _parse_time(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_text(text: str) -> str:
    """Убираем префикс автора «@user Имя:» из групповых чатов."""
    return _LEADING_AUTHOR.sub("", text or "").strip()


def _matches(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _is_system_feed(text: str) -> bool:
    """True, если сообщение — системный авто-лог кошелька (не человеческое)."""
    return _matches(_SYSTEM_FEED, text)


def _is_internal_note(text: str) -> bool:
    """True, если сообщение — только ссылка на Slack/Notion (внутренняя заметка)."""
    residue = _INTERNAL_LINK_RE.sub(" ", text)
    if residue == text:
        return False
    residue = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", residue)
    return len(residue) < 3


def _strip_closing(text: str) -> str:
    """Срезаем закрывающие вопросы вежливости («остались ли вопросы?»)."""
    out = text
    for p in _CLOSING_ASK:
        out = p.sub(" ", out)
    return out


_CLOSING_LEAD = re.compile(r"(подскажите|пожалуйста)[,!\s]*", re.IGNORECASE)


def _is_closing_only(text: str) -> bool:
    """True, если сообщение — только вопрос вежливости, без содержательной части.

    Такое сообщение не «запрос данных»: если клиент промолчал, тикет решён
    предыдущим ответом, а не «ждёт клиента» (руками: SG-060, SG-065, SG-083).
    """
    residue = _strip_closing(text)
    if residue == text:
        return False
    residue = _CLOSING_LEAD.sub(" ", residue)
    for p in _GREETING:
        residue = p.sub(" ", residue)
    residue = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", residue)
    return len(residue) < 3


_QUOTED_RE = re.compile(r"«[^»]{0,60}»")
_URL_RE = re.compile(r"https?://\S+")


def _is_support_ask(text: str) -> bool:
    """True, если поддержка запрашивает данные / задаёт уточняющий вопрос.

    Закрывающий вопрос вежливости («могу ли ещё чем-то помочь?») срезаем до
    проверки: он не делает ответ поддержки запросом данных. Также срезаем
    текст в «кавычках» и ссылки: название кнопки «Forgot your password?» и
    «?» в параметрах URL — не вопрос клиенту (руками: AZ-003).
    """
    cleaned = _URL_RE.sub(" ", _QUOTED_RE.sub(" ", _strip_closing(text)))
    return _matches(_SUPPORT_ASK, cleaned)


def _is_client_filler(m: "Msg") -> bool:
    """True, если это клиентский хвостовой филлер («подожду», «буду ждать»)."""
    return m.is_client and _matches(_CLIENT_FILLER, m.text)


def _last_substantive(msgs: list["Msg"], skip_filler: bool = False):
    """Последнее содержательное сообщение; при skip_filler пропускаем хвостовые
    клиентские филлеры («подожду») и отдельные закрывающие вопросы вежливости
    поддержки («остались ли вопросы?») — они не меняют, чей ход."""
    for x in reversed(msgs):
        if not x.substantive:
            continue
        if skip_filler and _is_client_filler(x):
            continue
        if skip_filler and x.is_support and _is_closing_only(x.text):
            continue
        return x
    return None


def _is_resolution(text: str, is_support: bool) -> bool:
    """Сообщение сигнализирует о РЕШЕНИИ (с учётом отрицаний).

    Отрицание проверяется ЛОКАЛЬНО, около найденного маркера, а не по всему
    сообщению: «Все вывел, через браузер не получалось» — это решение,
    хвост про старый способ его не отменяет (руками: AT-001).
    """
    if _is_system_feed(text):
        return False
    patterns = _RESOLUTION if is_support else _CLIENT_RESOLUTION
    for p in patterns:
        mt = p.search(text)
        if mt is None:
            continue
        window = text[max(0, mt.start() - 25):mt.end()]
        if _NEG_RESOLUTION.search(window):
            continue
        return True
    return False


def _is_stub(text: str) -> bool:
    """Шум/заглушка: целиковый шаблон-холд, либо только приветствие без сути.

    Сообщение с признаком решения (RESOLUTION_DONE/CLIENT_RESOLUTION) шумом не
    считается — решение важнее.
    """
    if not text.strip():
        return True
    if _is_system_feed(text):
        return True
    if _is_internal_note(text):
        return True
    if _is_resolution(text, True) or _is_resolution(text, False):
        return False
    if _matches(_HOLD, text):
        return True
    # чистое вложение без текста («[image: …]») — читать нечего, это не реплика
    residue = _ATTACHMENT_RE.sub(" ", text)
    for p in _GREETING:
        residue = p.sub(" ", residue)
    residue = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", residue)
    return len(residue) < 3


def normalize(raw_messages: Iterable[dict[str, Any]]) -> list[Msg]:
    """dict-сообщения → отсортированный, дедуплицированный список Msg."""
    msgs: list[Msg] = []
    seen: set[tuple] = set()
    for m in raw_messages:
        ts = _parse_time(m.get("time") if m.get("time") not in (None, "") else m.get("time_unix"))
        if ts is None:
            continue
        raw_text = m.get("text") or ""
        text = _clean_text(raw_text)
        direction = m.get("direction") or ("inbound" if m.get("type") == "inbound" else "outbound")
        # В группах CSM/аккаунт-менеджер приходит как inbound — реклассифицируем в support.
        # Проверяем ПО СЫРОМУ тексту: _clean_text уже срезал префикс "@handle Имя:".
        if direction == "inbound" and _group_author_is_support(raw_text):
            direction = "outbound"
        # chatapp: сотрудник опознаётся по author_id (роль в поле, не в тексте).
        if direction == "inbound" and str(m.get("author_id") or "").strip() in SUPPORT_AGENT_IDS:
            direction = "outbound"
        key = (ts.isoformat(), direction, text[:60])
        if key in seen:
            continue
        seen.add(key)
        msgs.append(
            Msg(
                ts=ts,
                direction=direction,
                text=text,
                is_stub=_is_stub(text),
                is_csat=_matches(_CSAT, text),
            )
        )
    msgs.sort(key=lambda x: x.ts)
    return msgs


def segment_incidents(msgs: list[Msg], gap_hours: float = INCIDENT_GAP_HOURS) -> list[list[Msg]]:
    """Режем переписку на инциденты по паузе между соседними сообщениями.

    Новый инцидент начинается ТОЛЬКО когда после длинной паузы пишет клиент —
    это новое обращение. Поздний ответ поддержки после паузы (клиент спросил
    вчера, поддержка ответила сегодня) — продолжение прежнего инцидента, а не
    новый: иначе сегмент открылся бы support-сообщением и дал ложный FR=нет.
    """
    if not msgs:
        return []
    segments: list[list[Msg]] = [[msgs[0]]]
    for prev, cur in zip(msgs, msgs[1:]):
        if (cur.ts - prev.ts) > timedelta(hours=gap_hours) and cur.is_client:
            segments.append([cur])
        else:
            segments[-1].append(cur)
    return segments


# ─────────────────────────────────────────────────────────────────────────────
# Рабочее время
# ─────────────────────────────────────────────────────────────────────────────

def working_seconds_sched(start: datetime, end: datetime,
                          work_start: time, work_end: time,
                          work_days: set) -> float:
    """Секунды рабочего времени между start и end по произвольному графику
    (work_start..work_end в WORK_TZ, только дни недели из work_days)."""
    if end <= start:
        return 0.0
    start = start.astimezone(WORK_TZ)
    end = end.astimezone(WORK_TZ)
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() in work_days:
            win_start = datetime.combine(day, work_start, tzinfo=WORK_TZ)
            win_end = datetime.combine(day, work_end, tzinfo=WORK_TZ)
            seg_start = max(start, win_start)
            seg_end = min(end, win_end)
            if seg_end > seg_start:
                total += (seg_end - seg_start).total_seconds()
        day += timedelta(days=1)
    return total


def working_seconds(start: datetime, end: datetime) -> float:
    """Секунды в графике поддержки (Пн–Пт WORK_START..WORK_END, WORK_TZ)."""
    return working_seconds_sched(start, end, WORK_START, WORK_END, WORK_DAYS)


# ─────────────────────────────────────────────────────────────────────────────
# Расчёт метрик одного инцидента
# ─────────────────────────────────────────────────────────────────────────────

def _extract_handoffs(msgs: list[Msg]) -> list[Handoff]:
    """Ищем в сообщениях поддержки открытие/закрытие передач в смежные отделы."""
    handoffs: list[Handoff] = []
    open_by_dept: dict[str, Handoff] = {}
    for m in msgs:
        if not m.is_support:
            continue
        # закрытие уже открытых передач: явный маркер возврата ИЛИ факт решения
        # (ответ из смежного отдела вернулся и вопрос закрыт).
        closes = (
            _matches(_HANDOFF_CLOSE, m.text)
            and not _matches(_HANDOFF_STILL_WAITING, m.text)
        ) or _is_resolution(m.text, True)
        if open_by_dept and closes:
            for dep, h in list(open_by_dept.items()):
                h.closed_at = m.ts
                handoffs.append(h)
                del open_by_dept[dep]
        # открытие новых — но НЕ на сообщении-резолюции и НЕ на сообщении с
        # маркером возврата. Упоминание отдела в тексте ответа («получили
        # ответ от финансового отдела, платёж зачислен») — это ответ отдела,
        # а не новая передача. Иначе фантомная передача остаётся открытой и
        # блокирует ролевое закрытие тикета (шаг 4b), время отдела теряется.
        if closes:
            continue
        for dep, pats in _HANDOFF_OPEN.items():
            if dep not in open_by_dept and _matches(pats, m.text):
                open_by_dept[dep] = Handoff(department=dep, opened_at=m.ts)
    # незакрытые передачи остаются open (ждём смежный отдел)
    handoffs.extend(open_by_dept.values())
    return handoffs


def compute_incident(msgs: list[Msg], index: int = 0) -> TicketMetrics:
    metrics = TicketMetrics(incident_index=index, n_messages=len(msgs))
    if not msgs:
        return metrics

    # 1) старт — первое содержательное входящее (обращение клиента)
    first_client = next((m for m in msgs if m.is_client and m.substantive), None)
    if first_client is None:
        first_client = next((m for m in msgs if m.is_client), None)
    if first_client is None:
        metrics.status = "no_client_message"
        return metrics
    metrics.started_at = first_client.ts

    # 2) first response — первая реакция живой поддержки после старта.
    #    Операторская заглушка «мы получили ваше сообщение» СЧИТАЕТСЯ: её шлёт
    #    оператор, и руководитель поддержки в ручной разметке берёт именно её.
    #    Не считаются авто-сообщения бота (приветствие, «дождитесь оператора»)
    #    и CSAT-опросы.
    #    Отсчёт от ПЕРВОГО сообщения клиента, даже несодержательного: ответ
    #    оператора на голое «Здравствуйте» — тоже первый ответ (руками: SG-008).
    any_client = next((m for m in msgs if m.is_client), first_client)
    resp_from = min(any_client.ts, first_client.ts)
    first_resp = next(
        (m for m in msgs
         if m.is_support and m.ts >= resp_from and m.text.strip()
         and not m.is_csat and not _matches(_BOT_AUTO, m.text)
         and not _is_system_feed(m.text) and not _is_internal_note(m.text)),
        None,
    )
    if first_resp:
        metrics.first_response_at = first_resp.ts
        # ответ раньше первого содержательного обращения (на «Здравствуйте») —
        # ожидание нулевое, отрицательным быть не должно
        metrics.first_response_seconds = max(
            0.0, (first_resp.ts - first_client.ts).total_seconds())

    # 3) хендоффы в смежные отделы. Суммы считаются ниже, после шага 4:
    #    момент решения может закрыть зависшую передачу (см. 4c).
    metrics.handoffs = _extract_handoffs(msgs)

    # 4) решение — приоритет у явного маркера «выполнено»/подтверждения клиента
    #    (берём последний по времени). Простое «спасибо» тут не в счёт.
    resolution_msg = None
    for m in msgs:
        if m.ts < first_client.ts:
            continue
        if _is_resolution(m.text, m.is_support):
            resolution_msg = m

    # клиент вернулся ПОСЛЕ «решения» с новым содержательным сообщением
    # (не «спасибо», не филлер) и остался без ответа — вопрос не закрыт
    # (руками: SG-087, SG-127).
    if resolution_msg is not None:
        last_sub = _last_substantive(msgs, skip_filler=True)
        if last_sub is not None and last_sub.ts > resolution_msg.ts \
                and last_sub.is_client:
            closes = ("?" not in last_sub.text) and (
                _matches(_CLIENT_CLOSE, last_sub.text)
                or _matches(_CLIENT_CONFIRM, last_sub.text)
                or _matches(_CLIENT_NEG_CLOSE, last_sub.text)
                or _matches(_CLIENT_ACK, last_sub.text)
            )
            if not closes:
                resolution_msg = None

    # 4a) явное закрытие клиентом важнее открытой передачи в отдел: если клиент
    #     ПОСЛЕДНИМ содержательным сообщением подтвердил закрытие («спасибо вам
    #     большое», «вопрос снят» …), тикет решён, даже если хендофф формально
    #     остался открытым (клиент удовлетворён — внутренняя передача уже не важна).
    if resolution_msg is None and any(h.closed_at is None for h in metrics.handoffs):
        last_sub = _last_substantive(msgs, skip_filler=True)
        if last_sub is not None and last_sub.ts >= first_client.ts \
                and last_sub.is_client and _matches(_CLIENT_CLOSE, last_sub.text) \
                and "?" not in last_sub.text \
                and not _NEG_RESOLUTION.search(last_sub.text):
            # «Ок, спасибо» сразу после «передали запрос в отдел» — вежливый
            # ответ на передачу, а не закрытие вопроса (руками: SG-097).
            prev_sup = next(
                (x for x in reversed(msgs)
                 if x.substantive and x.is_support and x.ts < last_sub.ts),
                None,
            )
            prev_opens_handoff = prev_sup is not None and any(
                _matches(pats, prev_sup.text) for pats in _HANDOFF_OPEN.values()
            )
            if not prev_opens_handoff:
                resolution_msg = last_sub

    # 4b) ролевое доопределение (тип A), если явного маркера нет и нет открытой
    #     передачи в смежный отдел. Диалог закрыт по факту, если последнее
    #     содержательное сообщение — либо ОТВЕТ поддержки (утверждение, не вопрос
    #     и не запрос данных), либо подтверждение закрытия от клиента.
    if resolution_msg is None and not any(h.closed_at is None for h in metrics.handoffs):
        last_sub = _last_substantive(msgs, skip_filler=True)
        if last_sub is not None and last_sub.ts >= first_client.ts:
            if last_sub.is_support and not _is_support_ask(last_sub.text):
                resolution_msg = last_sub
            elif last_sub.is_client and _matches(_CLIENT_CLOSE, last_sub.text) \
                    and "?" not in last_sub.text:
                # «Спасибо, жду. Есть примерные сроки?» — вопрос, не закрытие
                # (руками: SG-009)
                resolution_msg = last_sub
            elif last_sub.is_client and _matches(_CLIENT_NEG_CLOSE, last_sub.text) \
                    and not _NEG_RESOLUTION.search(last_sub.text):
                # клиент отрицает проблему в ответ на вопрос поддержки «ещё актуально?»
                prev_sup = next(
                    (x for x in reversed(msgs)
                     if x.substantive and not x.is_client and x.ts < last_sub.ts),
                    None,
                )
                if prev_sup is not None and _matches(_ACTUALITY_Q, prev_sup.text):
                    resolution_msg = last_sub
            elif last_sub.is_client and _matches(_CLIENT_CONFIRM, last_sub.text) \
                    and not _NEG_RESOLUTION.search(last_sub.text):
                # клиент подтвердил резюме/ответ поддержки («все так», «верно»):
                # закрытие, если пред. ход поддержки — реальный ответ либо
                # проверка актуальности (а не обычный уточняющий вопрос).
                prev_sup = next(
                    (x for x in reversed(msgs)
                     if x.substantive and not x.is_client and x.ts < last_sub.ts),
                    None,
                )
                if prev_sup is not None and not prev_sup.is_stub and (
                    _matches(_ACTUALITY_Q, prev_sup.text)
                    or not _is_support_ask(prev_sup.text)
                ):
                    resolution_msg = last_sub
            elif last_sub.is_client and _matches(_CLIENT_ACK, last_sub.text) \
                    and not _NEG_RESOLUTION.search(last_sub.text) \
                    and "?" not in last_sub.text:
                # клиент принял ответ («понятно/окей») — закрытие, только если
                # последний ход поддержки был реальным ОТВЕТОМ (не вопрос, не холд).
                # «Понял, а можно ли …?» — это НОВЫЙ вопрос, не закрытие
                # (руками: SG-108).
                prev_sup_any = next(
                    (x for x in reversed(msgs)
                     if not x.is_client and x.ts < last_sub.ts),
                    None,
                )
                if prev_sup_any is not None and not prev_sup_any.is_stub \
                        and not _is_support_ask(prev_sup_any.text):
                    resolution_msg = last_sub

    if resolution_msg:
        metrics.resolved_at = resolution_msg.ts
        metrics.resolution_seconds = (resolution_msg.ts - first_client.ts).total_seconds()
        metrics.resolution_working_seconds = working_seconds(first_client.ts, resolution_msg.ts)

    # 4c) решение закрывает зависшие передачи. Если тикет решён, ответ из
    #     смежного отдела по факту вернулся, даже когда скриптовой фразы
    #     возврата в переписке не было (клиент закрыл сам: «спасибо, всё
    #     получилось»). Без этого время в отделе терялось: список отделов
    #     заполнен, а часы по ним нулевые.
    if metrics.resolved_at is not None:
        for h in metrics.handoffs:
            if h.closed_at is None and h.opened_at <= metrics.resolved_at:
                h.closed_at = metrics.resolved_at

    # суммы по отделам — после 4c, иначе закрытые решением передачи не в счёт
    for h in metrics.handoffs:
        if h.seconds is not None:
            metrics.handoff_seconds_by_dept[h.department] = (
                metrics.handoff_seconds_by_dept.get(h.department, 0.0) + h.seconds
            )
            metrics.handoff_seconds_total += h.seconds

    # 5) статус
    metrics.status = _status(msgs, metrics)
    return metrics


def _status(msgs: list[Msg], m: TicketMetrics) -> str:
    if m.resolved_at is not None:
        return "resolved"
    if any(h.closed_at is None for h in m.handoffs):
        return "pending_department"
    # По последнему СОДЕРЖАТЕЛЬНОМУ сообщению определяем, чей ход (хвостовой
    #  клиентский филлер «подожду» игнорируем — он не требует ответа):
    #  клиент написал последним → ждём поддержку; поддержка → ждём клиента.
    last_sub = _last_substantive(msgs, skip_filler=True)
    if last_sub is not None:
        if last_sub.is_client:
            return "awaiting_support"
        if last_sub.is_support:
            return "awaiting_client"
    return "open"


# ─────────────────────────────────────────────────────────────────────────────
# Публичный API
# ─────────────────────────────────────────────────────────────────────────────

def analyze(raw_messages: Iterable[dict[str, Any]],
            gap_hours: float = INCIDENT_GAP_HOURS) -> list[TicketMetrics]:
    """Полный разбор диалога: нормализация → инциденты → метрики по каждому."""
    msgs = normalize(raw_messages)
    result: list[TicketMetrics] = []
    for seg in segment_incidents(msgs, gap_hours):
        # инцидент без содержательного обращения клиента — не тикет (хвосты
        # автозакрытий, служебные догоны). Пропускаем.
        if not any(m.is_client and m.substantive for m in seg):
            continue
        result.append(compute_incident(seg, len(result)))
    return result


def _fmt(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    mnt, sec = divmod(rem, 60)
    if h:
        return f"{h}ч {mnt}м"
    if mnt:
        return f"{mnt}м {sec}с"
    return f"{sec}с"


def render(metrics_list: list[TicketMetrics]) -> str:
    lines: list[str] = []
    for m in metrics_list:
        lines.append(f"── Инцидент #{m.incident_index} · статус: {m.status} · сообщений: {m.n_messages}")
        lines.append(f"   старт:            {m.started_at.astimezone(MSK).strftime('%d.%m %H:%M') if m.started_at else '—'} (MSK)")
        lines.append(f"   first response:   {_fmt(m.first_response_seconds)}")
        lines.append(f"   resolution (кал): {_fmt(m.resolution_seconds)}")
        lines.append(f"   resolution (раб): {_fmt(m.resolution_working_seconds)}")
        if m.handoffs:
            for h in m.handoffs:
                lines.append(f"   handoff [{h.department}]: {_fmt(h.seconds)}"
                             + ("" if h.closed_at else "  (не закрыт)"))
            lines.append(f"   handoff всего:    {_fmt(m.handoff_seconds_total)}")
        else:
            lines.append("   handoff:          нет")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Адаптер к таблице dialogs + CLI
# ─────────────────────────────────────────────────────────────────────────────

def _load_from_db(dialog_id: Optional[int], client_id: Optional[str]) -> list[dict[str, Any]]:
    from database import get_session, Dialog  # локальный импорт: CLI-режим
    with get_session() as db:
        q = db.query(Dialog)
        if dialog_id is not None:
            rows = [q.filter(Dialog.id == dialog_id).first()]
        else:
            rows = q.filter(Dialog.client_id == client_id).order_by(Dialog.dialog_date).all()
        raw: list[dict[str, Any]] = []
        for d in rows:
            if d and d.messages_json:
                raw.extend(json.loads(d.messages_json))
        return raw


def main() -> None:
    ap = argparse.ArgumentParser(description="Метрики времени обработки тикета")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dialog-id", type=int)
    src.add_argument("--client-id", type=str)
    src.add_argument("--file", type=str, help="JSON-массив сообщений")
    ap.add_argument("--gap-hours", type=float, default=INCIDENT_GAP_HOURS)
    ap.add_argument("--json", action="store_true", help="вывести результат в JSON")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = _load_from_db(args.dialog_id, args.client_id)

    result = analyze(raw, gap_hours=args.gap_hours)
    if args.json:
        print(json.dumps([m.to_dict() for m in result], ensure_ascii=False, indent=2))
    else:
        print(render(result))


if __name__ == "__main__":
    main()
