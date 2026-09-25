"""Local model backends. Nothing here talks to the internet.

- LlamaCppLLM: runs a GGUF model inside this Python process (llama-cpp-python). No server at all.
- OllamaLLM:   talks to the Ollama app on your own machine (127.0.0.1). Nothing leaves the computer.
- MockLLM:     no model; keyword rules. Only for testing the game loop.

Both real backends use schema-constrained decoding, so even small models return valid JSON."""
import json
import re
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def parse_json(raw: str):
    if raw is None:
        return None
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            return None
    return None


def clean_text(raw: str) -> str:
    return re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()


class LLM:
    name = "base"

    def chat(self, messages, schema=None, temperature=0.7, max_tokens=600) -> str:
        raise NotImplementedError


class LlamaCppLLM(LLM):
    name = "llama.cpp"

    def __init__(self, model_path, n_ctx=8192, n_gpu_layers=-1, chat_format=None):
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise LLMError("llama-cpp-python is not installed. Run: pip install llama-cpp-python "
                           "(see README for GPU builds), or use --backend ollama.") from e
        try:
            self.llm = Llama(model_path=str(model_path), n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                             chat_format=chat_format, verbose=False)
        except Exception as e:
            raise LLMError(f"Could not load model at {model_path}: {e}") from e

    def chat(self, messages, schema=None, temperature=0.7, max_tokens=600) -> str:
        kw = {"response_format": {"type": "json_object", "schema": schema}} if schema else {}
        out = self.llm.create_chat_completion(messages=messages, temperature=temperature,
                                              max_tokens=max_tokens, repeat_penalty=1.1, **kw)
        return out["choices"][0]["message"]["content"] or ""


class OllamaLLM(LLM):
    name = "ollama"

    def __init__(self, model, host="http://127.0.0.1:11434", n_ctx=8192, timeout=900):
        self.model, self.host, self.n_ctx, self.timeout = model, host.rstrip("/"), n_ctx, timeout

    def chat(self, messages, schema=None, temperature=0.7, max_tokens=600) -> str:
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"temperature": temperature, "num_ctx": self.n_ctx,
                            "num_predict": max_tokens, "repeat_penalty": 1.1}}
        if schema:
            body["format"] = schema
        req = urllib.request.Request(f"{self.host}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:300]
            if e.code == 404:
                raise LLMError(f"Ollama has no model '{self.model}'. Run: ollama pull {self.model}") from e
            raise LLMError(f"Ollama error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Can't reach Ollama at {self.host}. Start the Ollama app (or run 'ollama serve').") from e


class MockLLM(LLM):
    """Keyword-driven stand-in so you can test the whole loop with no model installed.
    It is intentionally dumb: real play needs a real local model."""
    name = "mock"

    RULES = [
        (r"\b(attack|hit|stab|slash|shoot|strike|swing|punch|kill|smash)\b", {"kind": "attack"}),
        (r"\bcast\b", {"kind": "spell"}),
        (r"\b(drink|quaff|use)\b.*\bpotion\b", {"kind": "item", "using": "potion of healing"}),
        (r"second wind", {"kind": "feature", "using": "second_wind"}),
        (r"action surge", {"kind": "feature", "using": "action_surge"}),
        (r"\b(dodge|defend)\b", {"kind": "dodge"}),
        (r"\b(hide|sneak)\b", {"kind": "hide", "dc": 12}),
        (r"\b(long rest|sleep|make camp)\b", {"kind": "rest", "rest": "long"}),
        (r"\b(short rest|catch our breath)\b", {"kind": "rest", "rest": "short"}),
        (r"\b(persuade|convince|negotiate|charm|ask nicely)\b", {"kind": "check", "ability": "CHA", "skill": "persuasion", "dc": 13}),
        (r"\b(lie|bluff|deceive)\b", {"kind": "check", "ability": "CHA", "skill": "deception", "dc": 14}),
        (r"\b(intimidate|threaten)\b", {"kind": "check", "ability": "CHA", "skill": "intimidation", "dc": 14}),
        (r"\b(search|investigate|examine|inspect)\b", {"kind": "check", "ability": "INT", "skill": "investigation", "dc": 12}),
        (r"\b(listen|spot|watch|scan)\b", {"kind": "check", "ability": "WIS", "skill": "perception", "dc": 12}),
        (r"\b(climb|jump|swim|force|break|lift|push)\b", {"kind": "check", "ability": "STR", "skill": "athletics", "dc": 13}),
        (r"\b(pick the lock|lockpick|pickpocket|steal)\b", {"kind": "check", "ability": "DEX", "skill": "sleight_of_hand", "dc": 14}),
        (r"\b(recall|remember)\b", {"kind": "check", "ability": "INT", "skill": "history", "dc": 12}),
        (r"\b(track|forage|navigate)\b", {"kind": "check", "ability": "WIS", "skill": "survival", "dc": 12}),
    ]

    def chat(self, messages, schema=None, temperature=0.7, max_tokens=600) -> str:
        user = messages[-1]["content"]
        props = (schema or {}).get("properties", {})
        if "actions" in props:
            return json.dumps({"actions": [self._adjudicate(n, t) for n, t in
                                           re.findall(r"^\d+\. ([^:\n]+): (.*)$", user.split("DECLARED ACTIONS:")[-1], re.M)]})
        if "effects" in props:
            resolved = re.findall(r"^- (.*)$", user.split("RESOLVED")[-1], re.M) if "RESOLVED" in user else []
            text = " ".join(resolved) if resolved else "The moment passes quietly as the world waits on your next move."
            return json.dumps({"narration": f"[mock GM] {text} What do you do?", "effects": []})
        if "location" in props:
            return json.dumps({
                "narration": "[mock GM] Rain drums on the slate roofs of Thornwick as you shelter in the Copper Kettle. "
                             "Across the room, the innkeeper Hesk argues with a mud-spattered courier about lights seen in the old quarry.",
                "location": {"name": "The Copper Kettle", "description": "A smoky roadside inn in the village of Thornwick."},
                "npcs": [{"name": "Hesk", "disposition": "friendly", "description": "the innkeeper, worried about the quarry"},
                         {"name": "Tamsin Vey", "disposition": "neutral", "description": "a courier who saw lights in the quarry"}],
                "quest": {"title": "Lights in the quarry", "notes": "Strange lights over the abandoned quarry north of Thornwick."}})
        return "[mock GM] " + re.sub(r"\s+", " ", user[-400:])

    def _adjudicate(self, name, text):
        low = text.lower()
        a = {"actor": name.strip(), "intent": text[:80], "kind": "none", "ability": "NONE", "skill": "none",
             "dc": 0, "advantage": "normal", "targets": [], "using": "", "slot_level": 0, "rest": "none"}
        for pattern, patch in self.RULES:
            if re.search(pattern, low):
                a.update(patch)
                break
        if a["kind"] == "spell":
            m = re.search(r"cast (?:a |the )?([a-z' ]+?)(?: at | on |$|\.)", low)
            a["using"] = m.group(1).strip() if m else ""
        if a["kind"] in ("attack", "spell"):
            a["targets"] = [w for w in re.findall(r"\b[A-Z][a-z]+(?: \d+)?\b", text) if w not in ("The", "A", "An")][:3]
        return a
