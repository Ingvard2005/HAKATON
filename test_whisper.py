from services.transcription import transcribe_audio


result = transcribe_audio("samples/test.mp3")

print("Язык:", result["language"])
print()

for segment in result["segments"]:
    print(
        f'[{segment["start"]} - {segment["end"]}] '
        f'{segment["text"]}'
    )