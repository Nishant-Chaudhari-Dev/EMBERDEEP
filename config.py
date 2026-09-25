"""Shared settings. Values come from config.json and can be overridden on the command line."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULTS = {"backend": "llamacpp", "model": "models/model.gguf", "ctx": 8192, "gpu_layers": -1,
            "ollama_host": "http://127.0.0.1:11434", "port": 8765}


def load_config(description: str, extra=None):
    cfg = dict(DEFAULTS)
    f = ROOT / "config.json"
    if f.exists():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--backend", choices=["llamacpp", "ollama", "mock"], default=cfg["backend"])
    p.add_argument("--model", default=cfg["model"], help="GGUF path (llamacpp) or model name (ollama)")
    p.add_argument("--ctx", type=int, default=cfg["ctx"], help="context window in tokens")
    p.add_argument("--gpu-layers", type=int, default=cfg["gpu_layers"], help="-1 = all on GPU, 0 = CPU only")
    p.add_argument("--ollama-host", default=cfg["ollama_host"])
    p.add_argument("--seed", type=int, default=None, help="fix the dice for reproducible games")
    p.add_argument("--load", default=None, help="path to a save file")
    p.add_argument("--debug", action="store_true")
    if extra:
        extra(p, cfg)
    return p.parse_args()


def build_llm(args):
    from gm.llm import LlamaCppLLM, MockLLM, OllamaLLM
    if args.backend == "mock":
        return MockLLM()
    if args.backend == "ollama":
        return OllamaLLM(args.model, host=args.ollama_host, n_ctx=args.ctx)
    path = Path(args.model)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        from gm.llm import LLMError
        raise LLMError(f"No model file at {path}. Put a .gguf model there (see README), "
                       f"or run with --backend ollama --model <name>, or --backend mock to test.")
    return LlamaCppLLM(path, n_ctx=args.ctx, n_gpu_layers=args.gpu_layers)
