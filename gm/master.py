"""The AI Game Master. Two calls per turn:
  1) adjudicate: free-form player text -> structured rule decisions (constrained JSON)
  2) narrate:    engine results -> story + proposed consequences (constrained JSON)
Anything the model gets wrong is caught by normalization here and validation in the engine."""
from engine import rules as R
from . import prompts as P
from .llm import LLMError, clean_text, parse_json


class GameMaster:
    def __init__(self, llm, engine, log_fn=None):
        self.llm = llm
        self.e = engine
        self.log_fn = log_fn or (lambda *_: None)

    def _json(self, system, user, schema, temperature, max_tokens):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(2):
            raw = self.llm.chat(msgs, schema=schema, temperature=temperature, max_tokens=max_tokens)
            data = parse_json(raw)
            if isinstance(data, dict):
                return data
            self.log_fn("bad JSON from model", raw[:300] if raw else raw)
            msgs = msgs + [{"role": "assistant", "content": raw or ""},
                           {"role": "user", "content": "That was not valid JSON. Reply with the JSON object only."}]
        return None

    # ------------------------------------------------------------------ adjudicate
    def adjudicate(self, declared) -> list:
        user = P.context(self.e, " ".join(t for _, t in declared)) + "\n\n" + P.declared_block(declared)
        if self.e.s.combat:
            user += "\n\nThis is combat: each actor gets one action this turn."
        data = None
        try:
            data = self._json(P.ADJUDICATE, user, P.ADJUDICATE_SCHEMA, 0.3, 160 + 170 * len(declared))
        except LLMError:
            raise
        except Exception as exc:
            self.log_fn("adjudicate failed", exc)
        raw = (data or {}).get("actions") or []
        out, used = [], set()
        for i, (pc, text) in enumerate(declared):
            j = next((k for k, a in enumerate(raw) if k not in used and isinstance(a, dict)
                      and str(a.get("actor", "")).lower() == pc.name.lower()), None)
            if j is None and i < len(raw) and i not in used and isinstance(raw[i], dict):
                j = i
            if j is not None:
                used.add(j)   # each model entry answers one character, even if two share a name
            out.append(self._normalize(raw[j] if j is not None else {}, pc, text))
        return out

    def _normalize(self, a, pc, text) -> dict:
        kind = a.get("kind") if a.get("kind") in P.KINDS else "none"
        skill = str(a.get("skill", "none")).lower().replace(" ", "_")
        skill = skill if skill in R.SKILLS else "none"
        ability = str(a.get("ability", "NONE")).upper()[:3]
        if ability not in R.ABILITIES:
            ability = R.SKILLS.get(skill, "NONE")
        if kind == "check" and ability == "NONE":
            ability = "WIS"
        dc = a.get("dc") if isinstance(a.get("dc"), int) else 0
        if kind in ("check", "hide") and not dc:
            dc = 15
        targets = a.get("targets") if isinstance(a.get("targets"), list) else []
        return {"actor": pc.name, "actor_id": pc.id, "text": text, "intent": str(a.get("intent", ""))[:120], "kind": kind,
                "ability": ability, "skill": skill, "dc": dc,
                "advantage": a.get("advantage") if a.get("advantage") in ("advantage", "disadvantage") else "normal",
                "targets": [str(t) for t in targets][:4], "using": str(a.get("using", ""))[:60],
                "slot_level": a.get("slot_level") if isinstance(a.get("slot_level"), int) else 0,
                "rest": a.get("rest") if a.get("rest") in ("short", "long") else "none"}

    # ------------------------------------------------------------------ narrate
    def narrate(self, declared, results, situation="") -> tuple:
        lines = [r["text"] for r in results if r.get("text")]
        user = P.context(self.e, " ".join(t for _, t in declared))
        if declared:
            user += "\n\n" + P.declared_block(declared)
        user += "\n\nRESOLVED (already applied, this is the truth):\n" + ("\n".join(f"- {l}" for l in lines) or "- (no rolls needed)")
        if situation:
            user += f"\n\nGM NOTE: {situation}"
        if not self.e.s.combat:
            user += "\n\n" + P.bestiary_hint(self.e)
        nxt = self.e.current_pc()
        if nxt:
            user += f"\n\nNext turn: {nxt.name}."
        data = self._json(P.NARRATE, user, P.NARRATE_SCHEMA, 0.85, 700)
        if not data:   # the results above are already applied, so never invite the player to act twice
            return "The scene blurs for a moment. (The GM lost the thread; the rolls above still happened, so carry on from here.)", []
        effects = [x for x in data.get("effects", []) if isinstance(x, dict)]
        return str(data.get("narration", "")).strip(), effects

    # ------------------------------------------------------------------ other calls
    def opening(self, premise) -> dict:
        user = f"PREMISE: {premise}\n\nPARTY:\n{P.party_block(self.e)}"
        return self._json(P.OPENING, user, P.OPENING_SCHEMA, 0.9, 900) or {}

    def summarize(self, old_summary, entries) -> str:
        events = "\n".join(f"{'Players' if x['role'] == 'players' else 'GM'}: {x['text']}" for x in entries)
        msgs = [{"role": "system", "content": P.SUMMARIZE},
                {"role": "user", "content": f"PREVIOUS SUMMARY:\n{old_summary or '(none)'}\n\nNEW EVENTS:\n{events}"}]
        return clean_text(self.llm.chat(msgs, temperature=0.3, max_tokens=320))[:1600]

    def ask(self, pc, question) -> str:
        msgs = [{"role": "system", "content": P.ASK},
                {"role": "user", "content": P.context(self.e, question) + f"\n\n{pc.name} asks: {question}"}]
        return clean_text(self.llm.chat(msgs, temperature=0.4, max_tokens=300))
