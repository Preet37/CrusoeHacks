"""Server configuration. Keep model identifiers in this module only."""
import os
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(SERVER_DIR / ".env")

CRUSOE_API_KEY = os.getenv("CRUSOE_API_KEY", "")
CRUSOE_BASE_URL = os.getenv("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

MODEL_RESPONDER = os.getenv("MODEL_RESPONDER", "nvidia/Nemotron-3.5-Lightning-30B-A3B")
MODEL_FAST = os.getenv("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
MODEL_OMNI = os.getenv("MODEL_OMNI", "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B")
MODEL_SMART = os.getenv("MODEL_SMART", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
MODEL_JEV = os.getenv("MODEL_JEV", "jev-latest")
MODEL_COACH = os.getenv("MODEL_COACH", "nvidia/NVIDIA-Nemotron-3.5-Super-VL")

TWIN_NAME = os.getenv("TWIN_NAME", "Twin")
CRUSOE_TIMEOUT = float(os.getenv("CRUSOE_TIMEOUT", "45"))
CRUSOE_MAX_RETRIES = int(os.getenv("CRUSOE_MAX_RETRIES", "3"))
CRUSOE_MAX_OUTPUT_TOKENS = int(os.getenv("CRUSOE_MAX_OUTPUT_TOKENS", "1200"))
CRUSOE_TOOL_TIMEOUT = float(os.getenv("CRUSOE_TOOL_TIMEOUT", "90"))
