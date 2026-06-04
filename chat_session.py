"""
In-memory chat session logic for the /chat and /slack emulators.

Per session_id we track:
  - accumulated text (for greeting detection across multiple messages)
  - waiting_for: a pending scenario follow-up (e.g. waiting for an email)

Priority on each new message:
  0. follow-up resolution (if a scenario is waiting)
  1. greeting → wait
  2. scenario detection → run or ask follow-up
  3. KB search → answer / no_match
"""

import auto_response
import scenarios

# {session_id: {"accumulated": str, "waiting_for": dict | None}}
# waiting_for shape: {"scenario_name": str, "data": dict, "hint": str}
_sessions: dict[str, dict] = {}


def reset(session_id: str) -> None:
    _sessions.pop(session_id, None)


def handle_message(session_id: str, text: str) -> dict:
    text = text.strip()
    if not text:
        return {"status": "empty"}

    session = _sessions.setdefault(
        session_id, {"accumulated": "", "waiting_for": None}
    )

    # 0. Resolving a pending follow-up
    if session["waiting_for"]:
        return _resolve_followup(session, text)

    # Append to accumulated text (used for greeting + scenario + KB)
    sep = " " if session["accumulated"] else ""
    session["accumulated"] = session["accumulated"] + sep + text
    accumulated = session["accumulated"]

    # 1. Greeting
    if auto_response._is_greeting(accumulated):
        return {
            "status": "waiting",
            "accumulated": accumulated,
            "hint": "Жду вопроса — напишите что именно вас интересует",
        }

    # 2. Scenario
    detection = scenarios.detect_scenario(accumulated)
    if detection:
        if detection.get("needs_followup"):
            session["waiting_for"] = {
                "scenario_name": detection["name"],
                "data": detection.get("data", {}),
                "hint": detection.get("followup_hint", "Ожидаю уточнение..."),
            }
            session["accumulated"] = ""
            return {
                "status": "waiting",
                "hint": session["waiting_for"]["hint"],
            }
        session["accumulated"] = ""
        return _scenario_response(detection)

    # 3. KB search
    result = auto_response.search(accumulated)
    article = result.get("article")
    if not article:
        return {
            "status": "no_match",
            "accumulated": accumulated,
            "hint": "Ответ в базе знаний не найден",
        }

    session["accumulated"] = ""
    return _article_response(article)


def _resolve_followup(session: dict, text: str) -> dict:
    waiting = session["waiting_for"]
    scenario = scenarios.get_scenario(waiting["scenario_name"])
    if scenario:
        completed = scenario.complete_followup(waiting.get("data", {}), text)
        if completed:
            session["waiting_for"] = None
            session["accumulated"] = ""
            return _scenario_response(completed)
    return {
        "status": "waiting",
        "hint": waiting.get("hint", "Ожидаю уточнение..."),
    }


def _scenario_response(detection: dict) -> dict:
    return {
        "status": "scenario_triggered",
        "scenario_name": detection["name"],
        "scenario_notification": detection["notification"],
        "scenario_informational": bool(detection.get("informational")),
    }


def _article_response(article: dict) -> dict:
    return {
        "status": "answer_found",
        "article_question": article.get("question", ""),
        "answer": article.get("answer", ""),
        "similarity": round(float(article.get("similarity", 0)), 3),
    }
