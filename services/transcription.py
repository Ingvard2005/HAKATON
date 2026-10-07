"""Replace services/transcription.py with this file's contents."""
import json
from pathlib import Path
import subprocess

AUDIO_ROOT = Path.home() / 'Documents' / 'CallMindAudio'
AUDIO_PYTHON = AUDIO_ROOT / '.venv' / 'Scripts' / 'python.exe'
PIPELINE = Path(r'C:\Users\PC\Documents\Codex\2026-10-06\asus-tuf-gaming-a16-2025-fa608um\outputs\callmind_site_pipeline.py')

def transcribe_audio(audio_path):
    if not AUDIO_PYTHON.is_file() or not PIPELINE.is_file():
        raise ValueError('Не найден Python аудиоокружения или скрипт CallMind')
    process = subprocess.run(
        [str(AUDIO_PYTHON), str(PIPELINE), str(Path(audio_path).resolve()),
         '--root', str(AUDIO_ROOT), '--transcribe-only'],
        cwd=str(AUDIO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', errors='replace', timeout=3600,
        env=_environment(),
    )
    if process.returncode:
        print(process.stdout)
        print(process.stderr)
        raise ValueError('Ошибка распознавания аудио. Подробности в терминале PyCharm: ' + process.stderr[-1500:])
    marker = 'CALLMIND_TRANSCRIPT='
    paths = [line[len(marker):] for line in process.stdout.splitlines() if line.startswith(marker)]
    if not paths:
        raise ValueError('Обработчик не вернул путь к расшифровке')
    call = json.loads(Path(paths[-1]).read_text(encoding='utf-8'))
    # Preserve legacy start/end keys for existing UI consumers.
    for segment in call['segments']:
        segment['start'] = segment['start_seconds']
        segment['end'] = segment['end_seconds']
    call['text'] = ' '.join(s['text'] for s in call['segments'])
    call['language'] = 'ru'
    return call

def _environment():
    import os
    result = os.environ.copy()
    result['PYTHONIOENCODING'] = 'utf-8'
    result['PYTHONUTF8'] = '1'
    return result
