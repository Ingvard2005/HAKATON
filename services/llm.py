from datetime import datetime
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()


class RiskItem(BaseModel):
    level: Literal[
        "low",
        "medium",
        "high"
    ]

    title: str
    explanation: str
    evidence: list[str]


class ClientRiskAnalysis(BaseModel):
    has_risk: bool

    risk_level: Literal[
        "none",
        "low",
        "medium",
        "high"
    ]

    summary: str

    risks: list[RiskItem]

class Agreement(BaseModel):
    description: str
    responsible: Literal["manager", "client", "unknown"]
    deadline: str | None = None
    deadline_original: str | None = None
    evidence: str


class CallAnalysis(BaseModel):
    summary: str
    agreements: list[Agreement]
    next_action: str | None = None
    follow_up_required: bool


MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]


def analyze_call(
    transcript: dict,
    call_datetime: datetime
) -> CallAnalysis:
    import json
    import urllib.request

    payload = {
        "started_at": call_datetime.isoformat(),
        "participants": transcript.get("participants", []),
        "segments": transcript["segments"],
    }

    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps({
            "model": "callmind-site",
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            ],
            "stream": False,
            "think": False,
            "format": CallAnalysis.model_json_schema(),
            "options": {
                "temperature": 0,
                "num_ctx": 8192,
            },
            "keep_alive": 0,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )

    with opener.open(request, timeout=1800) as response:
        result = json.load(response)

    return CallAnalysis.model_validate_json(
        result["message"]["content"]
    )

def analyze_client_history(
    history: list[dict],
    current_datetime: datetime
) -> ClientRiskAnalysis:
    client = genai.Client()

    history_text = ""

    for call in history:

        history_text += (
            "\n"
            "============================\n"
        )

        history_text += (
            f"Дата звонка: "
            f"{call['call_datetime']}\n"
        )

        history_text += (
            f"Краткое содержание: "
            f"{call['summary']}\n"
        )

        history_text += (
            f"Транскрипция:\n"
            f"{call['transcript']}\n"
        )

        history_text += (
            "\nДоговорённости:\n"
        )

        for agreement in call[
            "agreements"
        ]:

            history_text += (
                f"- {agreement['description']}\n"
                f"  Ответственный: "
                f"{agreement['responsible']}\n"
                f"  Срок: "
                f"{agreement['deadline']}\n"
                f"  Статус: "
                f"{agreement['status']}\n"
                f"  Подтверждение: "
                f"{agreement['evidence']}\n"
            )

    prompt = f"""
Ты анализируешь историю телефонных разговоров
между компанией и одним клиентом.

Текущее время:
{current_datetime.isoformat()}

История общения:

{history_text}

Нужно определить риски в работе с клиентом.

Особенно обращай внимание на:

- повторные обещания менеджера;
- обещания, срок которых уже прошёл;
- обещания, которые не были выполнены;
- случаи, когда клиент говорит,
  что ранее обещанное не получил;
- повторяющиеся жалобы клиента;
- ситуации, когда одно и то же действие
  обещается несколько раз;
- конфликтующие договорённости;
- потерю следующего шага;
- признаки ухудшения отношений с клиентом.

Очень важно:

Используй только информацию,
которая присутствует в истории.

Не придумывай события.

Если риска нет:

has_risk = false
risk_level = "none"

Уровни:

low — небольшой потенциальный риск;

medium — проблема требует внимания;

high — существует явное нарушение
или повторно невыполненное обязательство.

В evidence указывай конкретные фрагменты,
на основании которых обнаружен риск.
"""

    last_error = None

    for model in MODELS:

        try:

            print(
                f"Анализ истории: {model}"
            )

            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type=(
                        "application/json"
                    ),
                    response_schema=(
                        ClientRiskAnalysis
                    ),
                    temperature=0.1
                )
            )

            return (
                ClientRiskAnalysis
                .model_validate_json(
                    response.text
                )
            )

        except Exception as error:

            print(
                f"{model} недоступна: "
                f"{error}"
            )

            last_error = error

    raise RuntimeError(
        "Не удалось проанализировать "
        f"историю клиента: {last_error}"
    )
