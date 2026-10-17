"""Env + model ids. The only place model ids live.

Stub created by PREET-CLAUDE (PC-1). PREET-CODEX owns this file (PX-1).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(SERVER_DIR / ".env")

CRUSOE_API_KEY = os.getenv("CRUSOE_API_KEY", "")
CRUSOE_BASE_URL = os.getenv("CRUSOE_BASE_URL", "https://api.inference.crusoecloud.com/v1")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

MODEL_RESPONDER = os.getenv("MODEL_RESPONDER", "nvidia/nemotron-3.5-lightning-30b-a3b")
MODEL_FAST = os.getenv("MODEL_FAST", "nvidia/Nemotron-3-Nano-30B-A3B")
MODEL_SMART = os.getenv("MODEL_SMART", "nvidia/Nemotron-3-Super-120B-A12B")
MODEL_COACH = os.getenv("MODEL_COACH", "nvidia/Nemotron-3-Ultra-550B")

TWIN_NAME = os.getenv("TWIN_NAME", "Twin")
