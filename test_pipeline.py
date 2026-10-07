from datetime import datetime

from services.transcription import transcribe_audio
from services.llm import analyze_call


audio_path = "samples/test.mp3"


print("1. Распознаём аудио...")

transcription = transcribe_audio(audio_path)

print()
print("Транскрипция:")
print(transcription["text"])

print()
print("2. Анализируем разговор через Gemini...")

analysis = analyze_call(
    transcript=transcription["text"],
    call_datetime=datetime(
        2026,
        10,
        6,
        14,
        35
    )
)

print()
print("Результат:")

print(
    analysis.model_dump_json(
        indent=2
    )
)