"""Inference & Conversion: talks to the local llama.cpp engine (OpenAI-compatible API)."""
import json, os, re, time
import httpx

LLAMA_PORT = os.getenv("LLAMA_PORT", "18081")
LLAMA_URL = os.getenv("LLAMA_URL") or f"http://127.0.0.1:{LLAMA_PORT}"
TIMEOUT = float(os.getenv("DIAL0_LLM_TIMEOUT", "900"))  # one core: prompt processing is slow
TOOLS = ["click", "ref", "grep", "mcp", "final"]

# Agent-loop step (dial0 --agent). Bounded strings keep generation short on one core.
STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "maxLength": 160},
        "tool": {"type": "string", "enum": TOOLS},
        "input": {"type": "string", "maxLength": 400},
    },
    "required": ["thought", "tool", "input"],
    "additionalProperties": False,
}

# Fast path: the whole request in ONE call -> list of commands. "why" (optional) comes first so the model
# states its reasoning before choosing; it costs ~20 generated tokens.
def plan_schema(with_why: bool = True) -> dict:
    props = {}
    if with_why:
        props["why"] = {"type": "string", "maxLength": 200}
    props["commands"] = {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 6}
    props["search"] = {"type": "string", "maxLength": 80}  # keywords to look up when nothing in the reference fits
    props["note"] = {"type": "string", "maxLength": 300}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


PLAN_SCHEMA = plan_schema(True)

INSIGHTS_SCHEMA = {  # analysis of a workflow's findings + log excerpt
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 400},
        "causes": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 4},
        "next_steps": {"type": "array", "items": {"type": "string", "maxLength": 220}, "maxItems": 4},
    },
    "required": ["summary", "causes", "next_steps"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 600}},
    "required": ["answer"],
    "additionalProperties": False,
}


class LLMError(Exception):
    pass


def _json(text: str) -> dict:
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", (text or "").strip())
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError(f"model returned no JSON: {text[:200]!r}")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned invalid JSON: {e}") from e


def complete(messages: list[dict], schema: dict, max_tokens: int = 200) -> tuple[dict, dict]:
    """One grammar-constrained call. Returns (parsed JSON, timings)."""
    t0 = time.time()
    try:
        r = httpx.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": messages,
                "temperature": 0.1,
                "top_p": 0.8,
                "top_k": 20,
                "max_tokens": max_tokens,
                "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "out", "strict": True, "schema": schema}},
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        content = body["choices"][0]["message"]["content"] or ""
    except httpx.HTTPError as e:
        raise LLMError(f"inference engine error: {e}") from e
    t = body.get("timings") or {}
    timings = {"llm_s": round(time.time() - t0, 1),
               "prompt_tokens": t.get("prompt_n"), "prompt_s": round((t.get("prompt_ms") or 0) / 1000, 1),
               "cached_tokens": t.get("cache_n"),
               "gen_tokens": t.get("predicted_n"), "gen_s": round((t.get("predicted_ms") or 0) / 1000, 1)}
    return _json(content), timings


def _parse(text: str) -> dict:
    d = _json(text)
    if d.get("tool") not in TOOLS or not isinstance(d.get("input"), str):
        raise LLMError(f"model returned an invalid step: {str(d)[:200]}")
    d.setdefault("thought", "")
    return d


def next_step(messages: list[dict]) -> dict:
    d, _ = complete(messages, STEP_SCHEMA, max_tokens=300)
    return _parse(json.dumps(d))
