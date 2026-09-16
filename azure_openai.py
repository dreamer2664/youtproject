"""Azure OpenAI script lane (student-pack $100 credit) with hard budget guards.

Why this file exists: free lanes are trusted because they cannot spend money.
A paid lane must PROVE it cannot run away. Three independent proofs:

1. Azure-side (strongest): student subscriptions hold CREDIT, not a card —
   exhausted credit disables services. No bill can ever appear. The user
   keeps the portal login; the agent only ever holds an endpoint API key,
   which can spend TOKENS ONLY (no VMs, no services, no portal access).
2. Code-side: every call is priced from the pinned table below; per-day and
   per-month USD caps live in config (defaults $1/$5); hitting a cap raises
   BudgetExhausted, which the provider chain treats as "lane failed" and
   falls back to the free lanes automatically. Unknown models are REFUSED
   (fail-closed: unknown price = no spend), and every call is worst-case
   priced BEFORE any network happens.
3. Audit-side: every call appends {date, model, tokens, usd} to
   azure_spend.jsonl next to state.json; `python main.py costs` summarizes it.

Scale check (why the default caps are generous, not tight): a 65s script is
~1.5k tokens in / ~2.5k out. On gpt-4o-mini ($0.15/$0.60 per 1M) that is
about $0.002 per video — $1/day covers ~500 videos. The $100 credit at
5 videos/day lasts roughly a decade on mini, months even on full gpt-4o.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import requests

from scriptgen import GeminiProvider

API_VERSION_DEFAULT = "2024-08-01-preview"

# Pinned price table, USD per 1M tokens: (input, output). Pinned 2026-09-16.
# A deployment is PRICED BY ITS BASE MODEL (config ai.azure_model), never by
# its deployment name (which is arbitrary). Unknown model = lane refused.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
}

LEDGER_NAME = "azure_spend.jsonl"


class BudgetExhausted(RuntimeError):
    """A spend cap blocked the call — the chain falls back to free lanes."""


def ledger_path_for(cfg) -> Path:
    return cfg.state_file.parent / LEDGER_NAME


def is_supported_model(model: str) -> bool:
    return (model or "").strip() in PRICES


def read_spend(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            entries.append(row)
    return entries


def log_spend(path: Path, *, model: str, tag: str,
              in_tokens: int, out_tokens: int, usd: float) -> None:
    row = {"date": date.today().isoformat(),
           "ts": datetime.now().isoformat(timespec="seconds"),
           "model": model, "tag": tag,
           "in": int(in_tokens), "out": int(out_tokens), "usd": round(usd, 6)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def spent_on(entries: list[dict], day: str) -> float:
    return sum(float(entry.get("usd", 0) or 0) for entry in entries
               if entry.get("date") == day)


def spent_in_month(entries: list[dict], month: str) -> float:
    return sum(float(entry.get("usd", 0) or 0) for entry in entries
               if str(entry.get("date", ""))[:7] == month)


def price_for(model: str, in_tokens: int, out_tokens: int) -> float:
    inp, outp = PRICES[model]
    return in_tokens / 1_000_000 * inp + out_tokens / 1_000_000 * outp


def costs_report(path: Path) -> dict:
    entries = read_spend(path)
    today = date.today().isoformat()
    calls_today = sum(1 for entry in entries if entry.get("date") == today)
    return {"today_usd": round(spent_on(entries, today), 4),
            "month_usd": round(spent_in_month(entries, today[:7]), 4),
            "calls_today": calls_today, "calls_total": len(entries)}


class AzureOpenAIProvider(GeminiProvider):
    """GPT scripts on the student credit. Reuses Gemini's prompts/flow."""

    name = "azure"

    def __init__(self, *, endpoint: str, api_key: str, deployment: str,
                 model: str, api_version: str = API_VERSION_DEFAULT,
                 ledger_path: Path, max_usd_per_day: float = 1.0,
                 max_usd_per_month: float = 5.0, max_tokens: int = 4096) -> None:
        model = (model or "").strip()
        if model not in PRICES:
            raise RuntimeError(
                f"Azure model {model!r} has no pinned price — refusing to "
                f"spend blind. Set ai.azure_model to one of: "
                f"{', '.join(sorted(PRICES))}.")
        missing = [label for label, value in
                   (("endpoint", endpoint), ("api_key", api_key),
                    ("deployment", deployment))
                   if not (value or "").strip()]
        if missing:
            raise RuntimeError(
                f"Azure misconfigured (missing: {', '.join(missing)}).")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key.strip()
        self.deployment = deployment.strip()
        self.model = model
        self.api_version = (api_version or API_VERSION_DEFAULT).strip()
        self.ledger_path = ledger_path
        self.max_usd_per_day = max_usd_per_day
        self.max_usd_per_month = max_usd_per_month
        self.max_tokens = max_tokens

    def _chat(self, prompt: str, *, temperature: float = 0.7,
              json_mode: bool = False, tag: str = "azure") -> str:
        entries = read_spend(self.ledger_path)
        today = date.today().isoformat()
        day_spent = spent_on(entries, today)
        month_spent = spent_in_month(entries, today[:7])
        if day_spent >= self.max_usd_per_day:
            raise BudgetExhausted(
                f"Azure daily cap ${self.max_usd_per_day:.2f} reached "
                f"(${day_spent:.4f} spent) — free lanes take over.")
        if month_spent >= self.max_usd_per_month:
            raise BudgetExhausted(
                f"Azure monthly cap ${self.max_usd_per_month:.2f} reached "
                f"(${month_spent:.4f} spent) — free lanes take over.")
        # Worst-case pricing BEFORE any network: output length is unknown,
        # so assume the full max_tokens come back.
        in_est = max(1, len(prompt) // 4)
        worst = price_for(self.model, in_est, self.max_tokens)
        if day_spent + worst > self.max_usd_per_day:
            raise BudgetExhausted(
                f"Azure call (~${worst:.4f} worst case) would break the "
                f"${self.max_usd_per_day:.2f} daily cap — free lanes take over.")
        if month_spent + worst > self.max_usd_per_month:
            raise BudgetExhausted(
                f"Azure call (~${worst:.4f} worst case) would break the "
                f"${self.max_usd_per_month:.2f} monthly cap — free lanes take over.")

        body: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = (f"{self.endpoint}/openai/deployments/{self.deployment}"
               f"/chat/completions")
        try:
            response = requests.post(
                url, params={"api-version": self.api_version},
                headers={"api-key": self.api_key}, json=body, timeout=120)
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Azure unreachable ({exc}) — failing over.") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Azure HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"] or ""
            usage = payload.get("usage", {}) or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Azure returned an unreadable reply: {response.text[:200]}") from exc
        if not text.strip():
            raise RuntimeError("Azure returned empty content.")
        in_tok = int(usage.get("prompt_tokens", in_est))
        out_tok = int(usage.get("completion_tokens", 0))
        usd = price_for(self.model, in_tok, out_tok)
        log_spend(self.ledger_path, model=self.model, tag=tag,
                  in_tokens=in_tok, out_tokens=out_tok, usd=usd)
        print(f"  [azure] {tag}: {in_tok}+{out_tok} tokens = ${usd:.4f} "
              f"(today ${day_spent + usd:.2f} of ${self.max_usd_per_day:.2f})")
        return text

    def _post(self, payload: dict, tag: str = "script") -> dict:
        try:
            prompt = payload["contents"][0]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                f"unexpected internal payload shape: {str(payload)[:200]}")
        gen = payload.get("generationConfig", {}) or {}
        text = self._chat(
            prompt, temperature=gen.get("temperature", 0.7),
            json_mode=gen.get("responseMimeType") == "application/json", tag=tag)
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
