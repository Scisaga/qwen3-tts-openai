import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qwen3_tts_openai.audio_processing import AudioProcessor
from qwen3_tts_openai.config import Settings

if __name__ == "__main__":
    print(AudioProcessor(Settings.from_env()).self_check())
