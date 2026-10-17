"""Voice output: Magpie TTS, sentence splitting, ordered audio streaming."""
from voice.tts import SentenceSplitter, speak, speak_text, synthesize

__all__ = ["SentenceSplitter", "speak", "speak_text", "synthesize"]
