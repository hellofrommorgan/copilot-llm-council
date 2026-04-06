from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import shlex
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

COPILOT_BASE_URL = "https://api.githubcopilot.com"
DEFAULT_HEADERS = {
    "Editor-Version": "vscode/1.99.0",
    "User-Agent": "Hermes-Agent",
    "Openai-Intent": "conversation-panel",
}
DEFAULT_RUBRIC = [
    "accuracy",
    "completeness",
    "reasoning",
    "usefulness",
    "clarity",
    "uncertainty_calibration",
]
DEFAULT_ROLES = [
    "first_principles",
    "contrarian",
    "executor",
]
ROLE_BRIEFS = {
    "contrarian": "Assume the current idea or interpretation may be wrong. Hunt for failure modes, hidden downside, brittle assumptions, and reasons the apparent answer could backfire.",
    "first_principles": "Ignore inherited framing where useful. Re-derive the problem from underlying goals, constraints, incentives, and first principles.",
    "expansionist": "Look for upside, adjacent opportunities, asymmetric bets, and larger framing that the questioner may be missing.",
    "outsider": "Act as a sharp outsider with no inside context. Focus on what is legible from the evidence itself and what a neutral external observer would notice.",
    "executor": "Care primarily about what should happen next in the real world. Convert good ideas into practical sequencing, Monday-morning actions, and avoid vague strategy fluff.",
}
DEFAULT_PERSONAS = [
    {"id": "first_principles", "label": "First principles", "brief": ROLE_BRIEFS["first_principles"]},
    {"id": "contrarian", "label": "Contrarian", "brief": ROLE_BRIEFS["contrarian"]},
    {"id": "executor", "label": "Executor", "brief": ROLE_BRIEFS["executor"]},
]
LABELS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass
class CouncilMember:
    model: str
    transport: str
    endpoint: str
    role: str
    requested_model: str
    requested_transport: str
    requested_endpoint: str
    resolution_reason: str
    seat_id: str = ""
    role_label: str = ""
    role_brief_text: str = ""


class CopilotCouncilError(RuntimeError):
    pass


class ModelCallError(CopilotCouncilError):
    def __init__(self, message: str, *, attempts: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:limit] or "question"


def load_json(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def get_stage_timeout(cfg: Dict[str, Any], stage: str, default: int = 600) -> int:
    stage_cfg = cfg.get(stage, {}) or {}
    return int(stage_cfg.get("timeout_seconds", cfg.get("request_timeout_seconds", default)))


def get_retry_policy(cfg: Dict[str, Any]) -> Dict[str, Any]:
    retry_cfg = cfg.get("retry", {}) or {}
    max_attempts = max(1, int(retry_cfg.get("max_attempts", 3)))
    raw_backoff = retry_cfg.get("backoff_seconds", [2, 6])
    if isinstance(raw_backoff, (int, float)):
        raw_backoff = [raw_backoff]
    backoff = [float(x) for x in (raw_backoff or [])]
    return {"max_attempts": max_attempts, "backoff_seconds": backoff}


def get_review_card_config(cfg: Dict[str, Any]) -> Dict[str, int]:
    card_cfg = cfg.get("review_card", {}) or {}
    return {
        "max_chars": int(card_cfg.get("max_chars", 1800)),
        "max_paragraphs": int(card_cfg.get("max_paragraphs", 6)),
    }


def resolve_github_token() -> str:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        if token:
            return token
    except Exception:
        pass
    raise CopilotCouncilError("No GitHub token found in GH_TOKEN/GITHUB_TOKEN and `gh auth token` failed")


def get_active_github_identity() -> Dict[str, Any]:
    token = resolve_github_token()
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"token {token}", "User-Agent": "Hermes-Agent"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    return {
        "login": payload.get("login"),
        "id": payload.get("id"),
        "name": payload.get("name"),
    }


def infer_endpoint(model: str, explicit: Optional[str] = None, catalog_entry: Optional[Dict[str, Any]] = None) -> str:
    if explicit:
        if explicit not in {"responses", "chat_completions", "cli"}:
            raise ValueError(f"Unsupported explicit endpoint: {explicit}")
        return explicit
    supported = (catalog_entry or {}).get("supported_endpoints") or []
    if "/responses" in supported:
        return "responses"
    if "/chat/completions" in supported:
        return "chat_completions"
    lower = model.lower()
    if lower.startswith("claude-"):
        return "chat_completions"
    if lower.startswith("gpt-5") or "codex" in lower:
        return "responses"
    raise ValueError(f"Unable to infer endpoint for model: {model}")


def copilot_headers(token: str, content_type: str = "application/json") -> Dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = content_type
    return headers


def http_json(url: str, *, token: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers=copilot_headers(token),
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise CopilotCouncilError(f"HTTP {e.code} for {url}: {body[:1200]}") from e
    except urllib.error.URLError as e:
        raise CopilotCouncilError(f"Request failed for {url}: {e}") from e


def fetch_model_catalog(timeout: int = 60) -> Dict[str, Any]:
    token = resolve_github_token()
    return http_json(f"{COPILOT_BASE_URL}/models", token=token, timeout=timeout)


def catalog_to_map(catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    items = catalog.get("data", catalog if isinstance(catalog, list) else [])
    return {item["id"]: item for item in items if isinstance(item, dict) and item.get("id")}


def extract_text_from_responses_payload(payload: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
    if not chunks and payload.get("output_text"):
        return payload["output_text"]
    return "".join(chunks).strip()


def extract_text_from_chat_payload(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
                out.append(part.get("text", ""))
        return "".join(out).strip()
    return ""


def extract_usage_from_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    usage = payload.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def extract_usage_from_chat_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    usage = payload.get("usage") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def incomplete_response_reason(payload: Optional[Dict[str, Any]]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("status") != "incomplete":
        return ""
    details = payload.get("incomplete_details") or {}
    if isinstance(details, dict):
        return str(details.get("reason") or "").strip()
    return ""


def repair_common_json_issues(text: str) -> str:
    repaired = text
    repaired = re.sub(r'([\]}"0-9])\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*:', r'\1, "\2":', repaired)
    repaired = re.sub(r'("[^"]*")\s+("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1, \2', repaired)
    return repaired


def parse_json_candidate(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(repair_common_json_issues(text))


def extract_json_object(text: str) -> Dict[str, Any]:
    if not text:
        raise ValueError("Empty text")
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, flags=re.S)
    if fence:
        stripped = fence.group(1).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            obj, _ = decoder.raw_decode(candidate)
            return obj
        except json.JSONDecodeError:
            pass
        try:
            return parse_json_candidate(candidate)
        except json.JSONDecodeError:
            pass
    raise ValueError("Unable to extract valid JSON object from model output")


def role_brief(role: str) -> str:
    return ROLE_BRIEFS.get(role, ROLE_BRIEFS["outsider"])


def persona_label(role: str, explicit: Optional[str] = None) -> str:
    return explicit or role.replace("_", " ").title()


def normalize_persona_row(row: Dict[str, Any]) -> Dict[str, str]:
    persona_id = str(row.get("id") or row.get("role") or "").strip()
    if not persona_id:
        raise ValueError("persona rows must provide id")
    return {
        "id": persona_id,
        "label": str(row.get("label") or persona_label(persona_id)).strip(),
        "brief": str(row.get("brief") or row.get("generation_instructions") or role_brief(persona_id)).strip(),
    }


def get_persona_rows(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    raw = cfg.get("personas")
    if raw:
        return [normalize_persona_row(item) for item in raw]
    return [dict(item) for item in DEFAULT_PERSONAS]


def expand_roster_rows(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if cfg.get("models"):
        personas = get_persona_rows(cfg)
        rows: List[Dict[str, Any]] = []
        for model_row in cfg.get("models", []):
            for persona in personas:
                row = dict(model_row)
                row["role"] = persona["id"]
                row["role_label"] = persona["label"]
                row["role_brief"] = persona["brief"]
                row["seat_id"] = row.get("seat_id") or f"{row['model']}__{persona['id']}"
                rows.append(row)
        return rows

    rows = []
    for idx, row in enumerate(cfg.get("roster", [])):
        role = row.get("role", DEFAULT_ROLES[idx % len(DEFAULT_ROLES)])
        item = dict(row)
        item["role"] = role
        item["role_label"] = item.get("role_label") or item.get("label") or persona_label(role)
        item["role_brief"] = item.get("role_brief") or item.get("brief") or role_brief(role)
        item["seat_id"] = item.get("seat_id") or f"{item['model']}__{role}"
        rows.append(item)
    return rows


def get_summary_config(cfg: Dict[str, Any]) -> Dict[str, int]:
    raw = cfg.get("summary") or cfg.get("summary_config") or {}
    return {
        "max_chars": int(raw.get("max_chars", 12000)),
        "top_n": int(raw.get("top_n", 5)),
        "max_list_items": int(raw.get("max_list_items", 5)),
        "answer_chars": int(raw.get("answer_chars", 4000)),
        "question_chars": int(raw.get("question_chars", 500)),
    }


def compact_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rstrip()
    for sep in ["\n\n", ". ", "\n"]:
        if sep in clipped:
            clipped = clipped.rsplit(sep, 1)[0].rstrip()
            break
    return clipped + "\n\n[truncated]"


def unique_preserve(items: List[str]) -> List[str]:
    return list(dict.fromkeys([item for item in items if item]))


def group_roles_by_model(rows: List[Dict[str, Any]]) -> List[str]:
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        grouped.setdefault(row["model"], []).append(row.get("role", "unknown"))
    return [f"- {model}: {', '.join(roles)}" for model, roles in grouped.items()]


def should_retry_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, urllib.error.URLError, subprocess.TimeoutExpired)):
        return True
    message = str(error).lower()
    markers = [
        "timed out",
        "timeout",
        "request failed",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
    ]
    return any(marker in message for marker in markers)


def build_review_card(answer_text: str, *, max_chars: int = 1800, max_paragraphs: int = 6) -> str:
    cleaned = (answer_text or "").strip()
    if not cleaned:
        return ""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]
    priority_markers = (
        "recommendation",
        "strongest rationale",
        "key uncertainty",
        "next step",
        "supporting detail",
    )
    priority: List[str] = []
    regular: List[str] = []
    for block in blocks:
        low = block.lower()
        if any(marker in low for marker in priority_markers):
            priority.append(block)
        else:
            regular.append(block)
    ordered: List[str] = []
    seen = set()
    for block in priority + regular:
        if block not in seen:
            ordered.append(block)
            seen.add(block)
    selected: List[str] = []
    total_chars = 0
    for block in ordered:
        if len(selected) >= max_paragraphs:
            break
        remaining = max_chars - total_chars
        if remaining <= 0:
            break
        clipped = block
        if len(clipped) > remaining:
            clipped = clipped[: max(0, remaining - 3)].rstrip() + "..."
        selected.append(clipped)
        total_chars += len(clipped) + 2
    card = "\n\n".join(selected).strip()
    if not card:
        return cleaned[:max_chars].rstrip()
    return card


def summarize_for_review(answer_text: str, cfg: Dict[str, Any]) -> str:
    card_cfg = get_review_card_config(cfg)
    return build_review_card(answer_text, max_chars=card_cfg["max_chars"], max_paragraphs=card_cfg["max_paragraphs"])


def normalize_review_payload(payload: Dict[str, Any], *, labels: List[str], rubric: List[str]) -> Dict[str, Any]:
    ranking = payload.get("ranking")
    if ranking != list(ranking or []):
        raise ValueError("ranking must be a list")

    warnings: List[str] = []
    normalized_ranking: List[str] = []
    seen_labels = set()
    dropped_ranking_labels = []
    for label in ranking:
        if label not in labels:
            dropped_ranking_labels.append(label)
            continue
        if label in seen_labels:
            warnings.append(f"Dropped duplicate ranking label: {label}")
            continue
        seen_labels.add(label)
        normalized_ranking.append(label)
    if dropped_ranking_labels:
        warnings.append(f"Dropped unknown ranking labels: {dropped_ranking_labels}")
    if not normalized_ranking:
        raise ValueError("ranking must contain at least one recognized label")
    omitted_labels = [label for label in labels if label not in normalized_ranking]
    if omitted_labels:
        warnings.append(f"Reviewer omitted labels from ranking: {omitted_labels}")

    best_answer = payload.get("best_answer")
    if best_answer not in normalized_ranking:
        if best_answer is not None:
            warnings.append(f"best_answer {best_answer!r} not in normalized ranking; defaulted to top ranked answer")
        best_answer = normalized_ranking[0]

    raw_scores = payload.get("scores") or {}
    raw_critiques = payload.get("critique_by_answer") or {}
    if not isinstance(raw_scores, dict):
        raise ValueError("scores must be a dict")
    if not isinstance(raw_critiques, dict):
        raise ValueError("critique_by_answer must be a dict")

    dropped_score_labels = [label for label in raw_scores.keys() if label not in labels]
    dropped_critique_labels = [label for label in raw_critiques.keys() if label not in labels]
    if dropped_score_labels:
        warnings.append(f"Dropped unknown score labels: {dropped_score_labels}")
    if dropped_critique_labels:
        warnings.append(f"Dropped unknown critique labels: {dropped_critique_labels}")

    normalized_scores: Dict[str, Dict[str, int]] = {}
    normalized_critiques: Dict[str, str] = {}
    for label in normalized_ranking:
        row = raw_scores.get(label)
        if not isinstance(row, dict):
            warnings.append(f"Missing score row for {label}")
            continue
        dims: Dict[str, int] = {}
        valid_row = True
        for dim in rubric:
            if dim not in row:
                warnings.append(f"Dropped score row for {label}: missing {dim}")
                valid_row = False
                break
            value = row[dim]
            if not isinstance(value, int) or not (1 <= value <= 10):
                warnings.append(f"Dropped score row for {label}: invalid {dim}={value}")
                valid_row = False
                break
            dims[dim] = value
        if not valid_row:
            continue
        normalized_scores[label] = dims
        critique = raw_critiques.get(label)
        normalized_critiques[label] = "" if critique is None else str(critique)
        if critique is None:
            warnings.append(f"Missing critique for {label}; filled with empty string")

    disagreements = payload.get("unresolved_disagreements") or []
    if not isinstance(disagreements, list):
        raise ValueError("unresolved_disagreements must be a list")
    common_miss = payload.get("collective_blind_spot", payload.get("what_all_answers_missed", ""))
    if common_miss is None:
        common_miss = ""
    return {
        "ranking": normalized_ranking,
        "best_answer": best_answer,
        "best_answer_why": payload.get("best_answer_why", ""),
        "scores": normalized_scores,
        "critique_by_answer": normalized_critiques,
        "unresolved_disagreements": [str(x) for x in disagreements],
        "collective_blind_spot": str(common_miss).strip(),
        "normalization_warnings": warnings,
    }


def aggregate_reviews(reviews: List[Dict[str, Any]], *, labels: List[str], rubric: List[str]) -> Dict[str, Any]:
    by_answer: Dict[str, Dict[str, Any]] = {}
    for label in labels:
        by_answer[label] = {
            "borda_points": 0,
            "first_place_votes": 0,
            "review_count": 0,
            "dimension_sums": {dim: 0.0 for dim in rubric},
            "dimension_means": {},
            "overall_mean": 0.0,
            "critiques": [],
        }
    unresolved: List[str] = []
    blind_spots: List[str] = []
    warnings: List[str] = []
    for review in reviews:
        ranking = review["ranking"]
        n = len(ranking)
        for idx, label in enumerate(ranking):
            points = n - idx - 1
            by_answer[label]["borda_points"] += points
            if idx == 0:
                by_answer[label]["first_place_votes"] += 1
        present_labels = list(review.get("scores", {}).keys())
        for label in present_labels:
            by_answer[label]["review_count"] += 1
            for dim in rubric:
                by_answer[label]["dimension_sums"][dim] += review["scores"][label][dim]
            by_answer[label]["critiques"].append(review["critique_by_answer"].get(label, ""))
        unresolved.extend(review.get("unresolved_disagreements") or [])
        blind = (review.get("collective_blind_spot") or "").strip()
        if blind:
            blind_spots.append(blind)
        warnings.extend(review.get("normalization_warnings") or [])
    for label in labels:
        count = by_answer[label]["review_count"]
        means = {}
        if count:
            for dim in rubric:
                means[dim] = by_answer[label]["dimension_sums"][dim] / count
            by_answer[label]["overall_mean"] = sum(means.values()) / len(rubric)
        by_answer[label]["dimension_means"] = means
        by_answer[label].pop("dimension_sums", None)
    ranking = sorted(
        labels,
        key=lambda label: (
            -by_answer[label]["borda_points"],
            -by_answer[label]["first_place_votes"],
            -by_answer[label]["overall_mean"],
            label,
        ),
    )
    unresolved = sorted(dict.fromkeys(unresolved))
    blind_spots = list(dict.fromkeys(blind_spots))
    warnings = list(dict.fromkeys(warnings))
    return {
        "review_count": len(reviews),
        "ranking": ranking,
        "by_answer": by_answer,
        "unresolved_disagreements": unresolved,
        "collective_blind_spots": blind_spots,
        "collective_blind_spot": blind_spots[0] if blind_spots else "",
        "normalization_warnings": warnings,
    }


def build_generation_prompt(question: str, role: str, role_label: Optional[str] = None, role_brief_text: Optional[str] = None) -> str:
    role_label = role_label or persona_label(role)
    role_brief_text = role_brief_text or role_brief(role)
    return textwrap.dedent(
        f"""
        You are one member of a decision council. Your assigned persona is: {role_label} ({role}).

        Persona brief:
        {role_brief_text}

        Question:
        {question}

        Requirements:
        - Answer directly and technically.
        - Use your assigned persona strongly, but stay truthful.
        - Surface the most decision-relevant considerations, not generic advice.
        - Use these exact section headings:
          Recommendation:
          Strongest rationale:
          Key uncertainty:
          Next step:
          Supporting detail:
        - Keep the answer self-contained.
        - Do not mention any hidden review or council process.
        """
    ).strip()


def build_review_prompt(question: str, labeled_cards: Dict[str, str], rubric: List[str], role: str) -> str:
    rubric_text = ", ".join(rubric)
    answer_block = "\n\n".join([f"[{label}]\n{card}" for label, card in labeled_cards.items()])
    schema_lines = "\n".join([f'      "{dim}": 1-10,' for dim in rubric[:-1]] + [f'      "{rubric[-1]}": 1-10'])
    return textwrap.dedent(
        f"""
        You are reviewing anonymous candidate answer cards to the same question. Judge substance over style. Output JSON only.

        Your review lens is: {role}
        Role brief:
        {role_brief(role)}

        Question:
        {question}

        Rubric dimensions:
        {rubric_text}

        Candidate answer cards:
        {answer_block}

        Return JSON with this shape:
        {{
          "ranking": ["best_label", "next_label"],
          "best_answer": "label",
          "best_answer_why": "short explanation",
          "scores": {{
            "A": {{
{schema_lines}
            }}
          }},
          "critique_by_answer": {{
            "A": "short critique"
          }},
          "collective_blind_spot": "the most important thing all candidate answers missed or underweighted",
          "unresolved_disagreements": ["optional strings"]
        }}

        Rules:
        - Rank every label exactly once if possible; if one card is too weak or malformed to rank cleanly, rank the strongest recognized labels you can.
        - Use only the provided labels.
        - Scores must be integers from 1 to 10.
        - Keep `best_answer_why` and each critique to one sentence.
        - Keep `unresolved_disagreements` to at most 3 items.
        - `collective_blind_spot` should be concise and specific.
        - Return JSON only. No markdown fences.
        """
    ).strip()


def build_chairman_prompt(question: str, labeled_answers: Dict[str, str], aggregate: Dict[str, Any], requested_roster: List[Dict[str, Any]], resolved_roster: List[Dict[str, Any]]) -> str:
    answer_block = "\n\n".join([f"[{label}]\n{text}" for label, text in labeled_answers.items()])
    review_highlights = []
    for label in aggregate.get("ranking", []):
        item = aggregate["by_answer"].get(label, {})
        critique = item.get("critiques", [])[:2]
        review_highlights.append(
            f"- {label}: borda={item.get('borda_points', 0)}, first_place_votes={item.get('first_place_votes', 0)}, overall_mean={item.get('overall_mean', 0.0):.2f}, sample_critiques={critique}"
        )
    disagreements = aggregate.get("unresolved_disagreements") or []
    blind_spots = aggregate.get("collective_blind_spots") or []
    warnings = aggregate.get("normalization_warnings") or []
    review_highlights_text = "\n".join(review_highlights) if review_highlights else "- none"
    requested_text = "\n".join([f"- {item['model']} [{item.get('role','outsider')}] via {item.get('transport','copilot_api')}" for item in requested_roster]) or "- none"
    resolved_text = "\n".join([
        f"- requested={item['requested_model']} resolved={item['model']} role={item.get('role','outsider')} transport={item.get('transport')} reason={item.get('resolution_reason','configured')}"
        for item in resolved_roster
    ]) or "- none"
    return textwrap.dedent(
        f"""
        You are the chairman of a multi-model council. Produce the best final answer for the user.

        Question:
        {question}

        Anonymous candidate answers:
        {answer_block}

        Aggregate ranking:
        {aggregate.get('ranking', [])}

        Review highlights:
        {review_highlights_text}

        Collective blind spots:
        {blind_spots}

        Unresolved disagreements:
        {disagreements}

        Review normalization warnings:
        {warnings}

        Requested roster:
        {requested_text}

        Resolved runtime roster:
        {resolved_text}

        Instructions:
        - Write one strong final answer for the user.
        - Include a blunt assessment, start/stop/continue guidance when relevant, and a concrete next step.
        - Use the rankings, critiques, and blind-spot extraction as signals, not as a script.
        - If there is meaningful uncertainty or disagreement, say so briefly.
        - Do not produce JSON.
        """
    ).strip()


def call_via_copilot_cli(*, model: str, prompt: str, timeout: int) -> Dict[str, Any]:
    start = time.time()
    cmd = ["copilot", "-p", prompt, "-s", "--no-ask-user", "--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise CopilotCouncilError(f"copilot CLI failed for model {model}: {proc.stderr.strip() or proc.stdout.strip()}")
    latency_ms = int((time.time() - start) * 1000)
    return {
        "model": model,
        "transport": "copilot_cli",
        "endpoint": "cli",
        "ok": True,
        "answer_text": proc.stdout.strip(),
        "latency_ms": latency_ms,
        "usage": {},
        "raw_response": {"stdout": proc.stdout, "stderr": proc.stderr, "command": shlex.join(cmd)},
        "error": None,
    }


def call_model_once(*, model: str, transport: str, endpoint: str, prompt: str, max_output_tokens: int, temperature: Optional[float], timeout: int) -> Dict[str, Any]:
    if transport == "copilot_cli":
        return call_via_copilot_cli(model=model, prompt=prompt, timeout=timeout)
    token = resolve_github_token()
    start = time.time()
    if endpoint == "responses":
        payload: Dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        raw = http_json(f"{COPILOT_BASE_URL}/responses", token=token, payload=payload, timeout=timeout)
        text = extract_text_from_responses_payload(raw)
        usage = extract_usage_from_responses_payload(raw)
    elif endpoint == "chat_completions":
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        raw = http_json(f"{COPILOT_BASE_URL}/chat/completions", token=token, payload=payload, timeout=timeout)
        text = extract_text_from_chat_payload(raw)
        usage = extract_usage_from_chat_payload(raw)
    else:
        raise ValueError(f"Unsupported endpoint: {endpoint}")
    latency_ms = int((time.time() - start) * 1000)
    return {
        "model": model,
        "transport": transport,
        "endpoint": endpoint,
        "ok": True,
        "answer_text": text,
        "latency_ms": latency_ms,
        "usage": usage,
        "raw_response": raw,
        "error": None,
    }


def call_model(*, model: str, transport: str, endpoint: str, prompt: str, max_output_tokens: int, temperature: Optional[float], timeout: int, retry_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy = retry_policy or {"max_attempts": 1, "backoff_seconds": []}
    max_attempts = max(1, int(policy.get("max_attempts", 1)))
    backoff_seconds = list(policy.get("backoff_seconds", []))
    attempts: List[Dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        try:
            result = call_model_once(
                model=model,
                transport=transport,
                endpoint=endpoint,
                prompt=prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": True,
                    "model": model,
                    "transport": transport,
                    "endpoint": endpoint,
                    "latency_ms": result.get("latency_ms"),
                }
            )
            result["attempts"] = attempts
            result["attempt_count"] = len(attempts)
            return result
        except Exception as e:
            retryable = should_retry_error(e)
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "model": model,
                    "transport": transport,
                    "endpoint": endpoint,
                    "error": str(e),
                    "retryable": retryable,
                }
            )
            if attempt >= max_attempts or not retryable:
                raise ModelCallError(
                    f"Call failed for {model} via {transport}:{endpoint} after {attempt} attempt(s): {e}",
                    attempts=attempts,
                ) from e
            sleep_s = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)] if backoff_seconds else 0
            if sleep_s > 0:
                time.sleep(sleep_s)
    raise AssertionError("unreachable")


def smoke_one(model: str, transport: str, endpoint: str, timeout: int = 60, retry_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = call_model(
        model=model,
        transport=transport,
        endpoint=endpoint,
        prompt="Reply with exactly: PONG",
        max_output_tokens=40,
        temperature=0,
        timeout=timeout,
        retry_policy=retry_policy,
    )
    ok = resp["answer_text"].strip() == "PONG"
    return {
        "model": model,
        "transport": transport,
        "endpoint": endpoint,
        "ok": ok,
        "text": resp["answer_text"],
        "latency_ms": resp["latency_ms"],
        "attempt_count": resp.get("attempt_count"),
        "attempts": resp.get("attempts", []),
    }


def resolve_roster_member(row: Dict[str, Any], *, catalog_map: Optional[Dict[str, Dict[str, Any]]] = None, timeout: int = 60, retry_policy: Optional[Dict[str, Any]] = None) -> CouncilMember:
    requested_model = row["model"]
    requested_transport = row.get("transport", "copilot_api")
    requested_endpoint = row.get("endpoint", "responses")
    role = row.get("role", "outsider")
    role_label = row.get("role_label") or persona_label(role)
    role_brief_text = row.get("role_brief") or role_brief(role)
    seat_id = row.get("seat_id") or f"{requested_model}__{role}"
    candidates: List[Dict[str, Any]] = [{
        "model": requested_model,
        "transport": requested_transport,
        "endpoint": requested_endpoint,
        "reason": "configured",
    }]
    for fallback in list(row.get("fallbacks") or []):
        entry = dict(fallback)
        entry.setdefault("transport", "copilot_api")
        entry.setdefault("reason", f"fallback_from:{requested_model}")
        candidates.append(entry)
    should_probe = bool(row.get("probe_before_use", bool(row.get("fallbacks"))))
    errors: List[str] = []
    for candidate in candidates:
        model = candidate["model"]
        transport = candidate.get("transport", "copilot_api")
        catalog_entry = (catalog_map or {}).get(model)
        if transport == "copilot_api" and catalog_map is not None and model not in catalog_map:
            errors.append(f"{model}: not present in active Copilot catalog")
            continue
        try:
            endpoint = infer_endpoint(model, explicit=candidate.get("endpoint"), catalog_entry=catalog_entry) if transport != "copilot_cli" else candidate.get("endpoint", "cli")
        except Exception as e:
            errors.append(f"{model}: {e}")
            continue
        if should_probe:
            try:
                probe = smoke_one(model, transport, endpoint, timeout=min(45, timeout), retry_policy=retry_policy)
                if probe.get("ok"):
                    return CouncilMember(
                        model=model,
                        transport=transport,
                        endpoint=endpoint,
                        role=role,
                        requested_model=requested_model,
                        requested_transport=requested_transport,
                        requested_endpoint=requested_endpoint,
                        resolution_reason=candidate.get("reason", "configured"),
                        seat_id=seat_id,
                        role_label=role_label,
                        role_brief_text=role_brief_text,
                    )
                errors.append(f"{model}: smoke probe returned {probe.get('text')!r}")
            except Exception as e:
                errors.append(f"{model}: smoke probe failed: {e}")
            continue
        return CouncilMember(
            model=model,
            transport=transport,
            endpoint=endpoint,
            role=role,
            requested_model=requested_model,
            requested_transport=requested_transport,
            requested_endpoint=requested_endpoint,
            resolution_reason=candidate.get("reason", "configured"),
            seat_id=seat_id,
            role_label=role_label,
            role_brief_text=role_brief_text,
        )
    detail = "; ".join(errors) if errors else "no usable candidate"
    raise CopilotCouncilError(f"Unable to resolve roster entry {requested_model}: {detail}")


def ensure_roster(cfg: Dict[str, Any], catalog_map: Optional[Dict[str, Dict[str, Any]]] = None) -> List[CouncilMember]:
    roster = []
    timeout = get_stage_timeout(cfg, "generation")
    retry_policy = get_retry_policy(cfg)
    for row in expand_roster_rows(cfg):
        roster.append(resolve_roster_member(row, catalog_map=catalog_map, timeout=timeout, retry_policy=retry_policy))
    chairman = cfg.get("chairman") or {}
    if chairman:
        chairman["endpoint"] = infer_endpoint(chairman["model"], explicit=chairman.get("endpoint"), catalog_entry=(catalog_map or {}).get(chairman["model"]))
        chairman.setdefault("transport", "copilot_api")
    return roster


def requested_roster_rows(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for row in expand_roster_rows(cfg):
        rows.append(
            {
                "seat_id": row["seat_id"],
                "model": row["model"],
                "transport": row.get("transport", "copilot_api"),
                "endpoint": row.get("endpoint", "responses"),
                "role": row.get("role", "outsider"),
                "role_label": row.get("role_label") or persona_label(row.get("role", "outsider")),
            }
        )
    return rows


def resolved_roster_rows(roster: List[CouncilMember]) -> List[Dict[str, Any]]:
    return [
        {
            "seat_id": m.seat_id,
            "model": m.model,
            "transport": m.transport,
            "endpoint": m.endpoint,
            "role": m.role,
            "role_label": m.role_label or persona_label(m.role),
            "requested_model": m.requested_model,
            "requested_transport": m.requested_transport,
            "requested_endpoint": m.requested_endpoint,
            "resolution_reason": m.resolution_reason,
        }
        for m in roster
    ]


def review_candidates_for_member(candidate: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    review_fallbacks = cfg.get("review_fallbacks", {}) or {}
    requested_model = candidate["model"]
    plan = [
        {
            "model": candidate["model"],
            "transport": candidate["transport"],
            "endpoint": candidate["endpoint"],
            "reason": "configured",
        }
    ]
    for fallback in review_fallbacks.get(requested_model, []):
        row = dict(fallback)
        row.setdefault("transport", "copilot_api")
        row.setdefault("reason", f"review_fallback_from:{requested_model}")
        plan.append(row)
    return plan


def run_stage1(question: str, roster: List[CouncilMember], cfg: Dict[str, Any]) -> Dict[str, Any]:
    generation_cfg = cfg.get("generation", {}) or {}
    timeout = get_stage_timeout(cfg, "generation")
    retry_policy = get_retry_policy(cfg)
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(roster) or 1) as pool:
        future_map = {
            pool.submit(
                call_model,
                model=member.model,
                transport=member.transport,
                endpoint=member.endpoint,
                prompt=build_generation_prompt(question, member.role, role_label=member.role_label, role_brief_text=member.role_brief_text),
                max_output_tokens=int(generation_cfg.get("max_output_tokens", 900)),
                temperature=generation_cfg.get("temperature"),
                timeout=timeout,
                retry_policy=retry_policy,
            ): member
            for member in roster
        }
        for future in concurrent.futures.as_completed(future_map):
            member = future_map[future]
            try:
                item = future.result()
                item["role"] = member.role
                item["role_label"] = member.role_label or persona_label(member.role)
                item["seat_id"] = member.seat_id
                item["requested_model"] = member.requested_model
                item["resolution_reason"] = member.resolution_reason
                item["review_card"] = summarize_for_review(item.get("answer_text", ""), cfg)
                results.append(item)
            except Exception as e:
                failures.append(
                    {
                        "seat_id": member.seat_id,
                        "model": member.model,
                        "transport": member.transport,
                        "endpoint": member.endpoint,
                        "role": member.role,
                        "role_label": member.role_label or persona_label(member.role),
                        "requested_model": member.requested_model,
                        "resolution_reason": member.resolution_reason,
                        "ok": False,
                        "answer_text": "",
                        "latency_ms": None,
                        "usage": {},
                        "raw_response": None,
                        "error": str(e),
                        "attempts": getattr(e, "attempts", []),
                    }
                )
    results.sort(key=lambda x: (x.get("seat_id") or f"{x['model']}__{x['role']}"))
    rng = random.Random(cfg.get("random_seed"))
    labels = LABELS[: len(results)]
    rng.shuffle(labels)
    for idx, item in enumerate(results):
        item["label"] = labels[idx]
    labeled_answers = {item["label"]: item["answer_text"] for item in results}
    labeled_review_cards = {item["label"]: item["review_card"] for item in results}
    return {
        "candidates": results,
        "failures": failures,
        "labeled_answers": labeled_answers,
        "labeled_review_cards": labeled_review_cards,
    }


def run_stage2_peer(question: str, stage1: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    review_cfg = cfg.get("review", {}) or {}
    rubric = cfg.get("rubric") or DEFAULT_RUBRIC
    timeout = get_stage_timeout(cfg, "review")
    retry_policy = get_retry_policy(cfg)
    valid_reviews: List[Dict[str, Any]] = []
    review_failures: List[Dict[str, Any]] = []
    review_substitutions: List[Dict[str, Any]] = []
    for candidate in stage1["candidates"]:
        reviewer_requested_model = candidate["model"]
        reviewer_role = candidate.get("role", "outsider")
        labeled_cards = dict(stage1["labeled_review_cards"])
        if review_cfg.get("exclude_self", True):
            labeled_cards.pop(candidate["label"], None)
        labels = sorted(labeled_cards.keys())
        if len(labels) < 2:
            continue
        prompt = build_review_prompt(question, {label: labeled_cards[label] for label in labels}, rubric, reviewer_role)
        fallback_events: List[Dict[str, Any]] = []
        normalized: Optional[Dict[str, Any]] = None
        for reviewer in review_candidates_for_member(candidate, cfg):
            review_max_tokens = int(review_cfg.get("max_output_tokens", 900))
            for review_attempt in range(2):
                try:
                    resp = call_model(
                        model=reviewer["model"],
                        transport=reviewer["transport"],
                        endpoint=reviewer["endpoint"],
                        prompt=prompt,
                        max_output_tokens=review_max_tokens,
                        temperature=review_cfg.get("temperature", 0),
                        timeout=timeout,
                        retry_policy=retry_policy,
                    )
                    payload = extract_json_object(resp["answer_text"])
                    normalized = normalize_review_payload(payload, labels=labels, rubric=rubric)
                    normalized["reviewer_model"] = reviewer["model"]
                    normalized["reviewer_transport"] = reviewer["transport"]
                    normalized["reviewer_endpoint"] = reviewer["endpoint"]
                    normalized["reviewer_role"] = reviewer_role
                    normalized["reviewer_requested_model"] = reviewer_requested_model
                    normalized["review_resolution_reason"] = reviewer.get("reason", "configured")
                    normalized["raw_response"] = resp["raw_response"]
                    normalized["attempts"] = resp.get("attempts", [])
                    normalized["fallback_events"] = fallback_events
                    if reviewer.get("reason") != "configured":
                        review_substitutions.append(
                            {
                                "requested_reviewer_model": reviewer_requested_model,
                                "resolved_reviewer_model": reviewer["model"],
                                "transport": reviewer["transport"],
                                "endpoint": reviewer["endpoint"],
                                "reason": reviewer.get("reason", "configured"),
                            }
                        )
                    break
                except Exception as e:
                    reason = incomplete_response_reason(locals().get("resp", {}).get("raw_response") if isinstance(locals().get("resp"), dict) else None)
                    fallback_events.append(
                        {
                            "reviewer_model": reviewer["model"],
                            "transport": reviewer["transport"],
                            "endpoint": reviewer["endpoint"],
                            "reason": reviewer.get("reason", "configured") if review_attempt == 0 else f"{reviewer.get('reason', 'configured')}:retry_after_{reason or 'parse_failure'}",
                            "error": str(e),
                            "attempts": getattr(e, "attempts", []),
                            "max_output_tokens": review_max_tokens,
                            "incomplete_reason": reason,
                        }
                    )
                    if reason == "max_output_tokens" and review_attempt == 0:
                        review_max_tokens = max(review_max_tokens + 400, int(review_max_tokens * 1.5))
                        resp = None
                        continue
                    break
            if normalized is not None:
                break
        if normalized is not None:
            valid_reviews.append(normalized)
        else:
            review_failures.append(
                {
                    "reviewer_model": reviewer_requested_model,
                    "reviewer_role": reviewer_role,
                    "error": f"All review candidates failed for {reviewer_requested_model}",
                    "fallback_events": fallback_events,
                }
            )
    aggregate = aggregate_reviews(valid_reviews, labels=sorted(stage1["labeled_answers"].keys()), rubric=rubric) if valid_reviews else {
        "review_count": 0,
        "ranking": list(stage1["labeled_answers"].keys()),
        "by_answer": {},
        "unresolved_disagreements": [],
        "collective_blind_spots": [],
        "collective_blind_spot": "",
        "normalization_warnings": [],
    }
    return {
        "reviews": valid_reviews,
        "review_failures": review_failures,
        "review_substitutions": review_substitutions,
        "aggregate": aggregate,
    }


def run_stage2_judge(question: str, stage1: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    rubric = cfg.get("rubric") or DEFAULT_RUBRIC
    chairman = cfg["chairman"]
    timeout = get_stage_timeout(cfg, "review")
    retry_policy = get_retry_policy(cfg)
    labels = sorted(stage1["labeled_review_cards"].keys())
    prompt = build_review_prompt(question, {label: stage1["labeled_review_cards"][label] for label in labels}, rubric, "outsider")
    try:
        resp = call_model(
            model=chairman["model"],
            transport=chairman.get("transport", "copilot_api"),
            endpoint=chairman["endpoint"],
            prompt=prompt,
            max_output_tokens=int(cfg.get("review", {}).get("max_output_tokens", 900)),
            temperature=cfg.get("review", {}).get("temperature", 0),
            timeout=timeout,
            retry_policy=retry_policy,
        )
        payload = extract_json_object(resp["answer_text"])
        normalized = normalize_review_payload(payload, labels=labels, rubric=rubric)
        normalized["reviewer_model"] = chairman["model"]
        normalized["reviewer_transport"] = chairman.get("transport", "copilot_api")
        normalized["reviewer_role"] = "outsider"
        normalized["raw_response"] = resp["raw_response"]
        normalized["attempts"] = resp.get("attempts", [])
        aggregate = aggregate_reviews([normalized], labels=labels, rubric=rubric)
        return {"reviews": [normalized], "review_failures": [], "review_substitutions": [], "aggregate": aggregate}
    except Exception as e:
        return {
            "reviews": [],
            "review_failures": [{"reviewer_model": chairman["model"], "error": str(e), "attempts": getattr(e, "attempts", [])}],
            "review_substitutions": [],
            "aggregate": {
                "review_count": 0,
                "ranking": labels,
                "by_answer": {},
                "unresolved_disagreements": [],
                "collective_blind_spots": [],
                "collective_blind_spot": "",
                "normalization_warnings": [],
            },
        }


def run_stage3_chairman(question: str, stage1: Dict[str, Any], stage2: Dict[str, Any], cfg: Dict[str, Any], requested_roster: List[Dict[str, Any]], resolved_roster: List[Dict[str, Any]]) -> Dict[str, Any]:
    chairman = cfg["chairman"]
    timeout = get_stage_timeout(cfg, "synthesis")
    retry_policy = get_retry_policy(cfg)
    prompt = build_chairman_prompt(question, stage1["labeled_answers"], stage2["aggregate"], requested_roster, resolved_roster)
    synthesis_max_tokens = int(cfg.get("synthesis", {}).get("max_output_tokens", 4096))
    synthesis_temperature = cfg.get("synthesis", {}).get("temperature", 0.2)
    all_attempts: List[Dict[str, Any]] = []
    for synthesis_attempt in range(3):
        try:
            resp = call_model(
                model=chairman["model"],
                transport=chairman.get("transport", "copilot_api"),
                endpoint=chairman["endpoint"],
                prompt=prompt,
                max_output_tokens=synthesis_max_tokens,
                temperature=synthesis_temperature,
                timeout=timeout,
                retry_policy=retry_policy,
            )
            all_attempts.extend(resp.get("attempts", []))
            reason = incomplete_response_reason(resp.get("raw_response"))
            if reason == "max_output_tokens" and synthesis_attempt < 2:
                synthesis_max_tokens = max(synthesis_max_tokens + 2048, int(synthesis_max_tokens * 1.5))
                continue
            return {
                "ok": True,
                "model": chairman["model"],
                "transport": chairman.get("transport", "copilot_api"),
                "endpoint": chairman["endpoint"],
                "final_answer": resp["answer_text"],
                "raw_response": resp["raw_response"],
                "attempts": all_attempts,
                "error": None,
                "truncated": bool(reason),
                "synthesis_max_tokens_used": synthesis_max_tokens,
            }
        except Exception as e:
            all_attempts.extend(getattr(e, "attempts", []))
            ranking = stage2.get("aggregate", {}).get("ranking") or list(stage1["labeled_answers"].keys())
            fallback_label = ranking[0] if ranking else None
            fallback_answer = stage1["labeled_answers"].get(fallback_label, "") if fallback_label else ""
            return {
                "ok": False,
                "model": chairman["model"],
                "transport": chairman.get("transport", "copilot_api"),
                "endpoint": chairman["endpoint"],
                "final_answer": fallback_answer,
                "raw_response": None,
                "attempts": all_attempts,
                "error": str(e),
                "fallback_label": fallback_label,
            }
    return {
        "ok": True,
        "model": chairman["model"],
        "transport": chairman.get("transport", "copilot_api"),
        "endpoint": chairman["endpoint"],
        "final_answer": resp["answer_text"],
        "raw_response": resp.get("raw_response"),
        "attempts": all_attempts,
        "error": None,
        "truncated": True,
        "synthesis_max_tokens_used": synthesis_max_tokens,
    }


def render_summary_markdown(result: Dict[str, Any]) -> str:
    cfg = get_summary_config(result)
    cfg.update(result.get("summary_config") or {})
    requested = result.get("requested_roster", [])
    resolved = result.get("resolved_roster", [])
    aggregate = result.get("stage2", {}).get("aggregate", {})
    ranking = aggregate.get("ranking") or []
    top_n = max(1, cfg["top_n"])
    max_items = max(1, cfg["max_list_items"])
    lines: List[str] = []
    identity = result.get("github_identity") or {}
    model_count = len(unique_preserve([item["model"] for item in requested]))
    persona_count = len(unique_preserve([item.get("role") for item in requested]))
    seat_count = len(requested)
    successful = len(result.get("stage1", {}).get("candidates", []))
    failed = len(result.get("stage1", {}).get("failures", []))

    lines.append("# Copilot LLM Council Run")
    lines.append("")
    lines.append(f"- Timestamp: {result.get('timestamp')}")
    lines.append(f"- GitHub identity: {identity.get('login')}")
    lines.append(f"- Mode: {result.get('mode')}")
    lines.append(f"- Requested matrix: {model_count} models × {persona_count} personas = {seat_count} seats")
    lines.append(f"- Successful seats: {successful}/{seat_count}")
    if failed:
        lines.append(f"- Failed seats: {failed}")
    lines.append(f"- Question: {compact_text(result.get('question', ''), cfg['question_chars'])}")
    lines.append("")

    lines.append("## Requested matrix")
    lines.extend(group_roles_by_model(requested))
    lines.append("")

    substitutions = [
        item for item in resolved
        if item.get("requested_model") != item.get("model") or item.get("resolution_reason") not in {None, "configured"}
    ]
    lines.append("## Runtime roster")
    if substitutions:
        for item in substitutions[:max_items]:
            lines.append(f"- {item['seat_id']}: requested={item['requested_model']} resolved={item['model']} via {item['transport']} reason={item['resolution_reason']}")
        if len(substitutions) > max_items:
            lines.append(f"- ... {len(substitutions) - max_items} more substitutions")
    else:
        lines.append("- All seats resolved as requested")
    lines.append("")

    lines.append("## Top ranked seats")
    if ranking:
        for idx, label in enumerate(ranking[:top_n], start=1):
            candidate = next((c for c in result.get('stage1', {}).get('candidates', []) if c.get('label') == label), None)
            item = aggregate.get('by_answer', {}).get(label, {})
            if candidate:
                lines.append(
                    f"{idx}. {label} — {candidate.get('seat_id')} | borda={item.get('borda_points', 0)} | first_place={item.get('first_place_votes', 0)} | mean={item.get('overall_mean', 0.0):.2f}"
                )
            else:
                lines.append(f"{idx}. {label}")
    else:
        lines.append("- No ranking available")
    lines.append("")

    blind_spots = unique_preserve(aggregate.get("collective_blind_spots") or ([aggregate.get("collective_blind_spot")] if aggregate.get("collective_blind_spot") else []))
    if blind_spots:
        lines.append("## Collective blind spots")
        for item in blind_spots[:max_items]:
            lines.append(f"- {item}")
        lines.append("")

    disagreements = unique_preserve(aggregate.get("unresolved_disagreements") or [])
    if disagreements:
        lines.append("## Unresolved disagreements")
        for item in disagreements[:max_items]:
            lines.append(f"- {item}")
        if len(disagreements) > max_items:
            lines.append(f"- ... {len(disagreements) - max_items} more")
        lines.append("")

    failures = result.get("failures") or []
    if failures:
        lines.append("## Failures")
        for item in failures[:max_items]:
            if isinstance(item, dict):
                seat = item.get("seat_id") or item.get("reviewer_model") or item.get("chairman_error") or item.get("model") or "failure"
                err = item.get("error") or item.get("chairman_error") or str(item)
                lines.append(f"- {seat}: {compact_text(str(err), 180)}")
            else:
                lines.append(f"- {compact_text(str(item), 180)}")
        if len(failures) > max_items:
            lines.append(f"- ... {len(failures) - max_items} more")
        lines.append("")

    remaining_budget = max(400, cfg["max_chars"] - sum(len(line) + 1 for line in lines) - 64)
    final_answer_chars = min(cfg["answer_chars"], remaining_budget, max(400, cfg["max_chars"] // 12))
    lines.append("## Final answer")
    lines.append("")
    lines.append(compact_text(result.get("stage3", {}).get("final_answer", ""), final_answer_chars))

    summary = "\n".join(lines).strip() + "\n"
    if len(summary) > cfg["max_chars"]:
        summary = compact_text(summary, cfg["max_chars"]) + "\n"
    return summary


def write_artifacts(result: Dict[str, Any], artifact_root: str | Path) -> Dict[str, str]:
    ts = result["timestamp"].replace(":", "").replace("-", "")
    slug = slugify(result["question"])
    run_dir = Path(artifact_root) / f"{ts}--{slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    summary_path = run_dir / "summary.md"
    compact_summary = result.get("compact_summary") or render_summary_markdown(result)
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    summary_path.write_text(compact_summary)
    return {"run_dir": str(run_dir), "result_json": str(result_path), "summary_md": str(summary_path)}


def run_council(question: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    catalog = fetch_model_catalog(timeout=min(60, int(cfg.get("request_timeout_seconds", 600))))
    catalog_map = catalog_to_map(catalog)
    roster = ensure_roster(cfg, catalog_map=catalog_map)
    requested = requested_roster_rows(cfg)
    resolved = resolved_roster_rows(roster)
    stage1 = run_stage1(question, roster, cfg)
    mode = cfg.get("mode", "peer")
    min_successful = int((cfg.get("runtime") or {}).get("min_successful_seats", 3))
    success_count = len(stage1["candidates"])
    if success_count == 0:
        raise CopilotCouncilError("No stage-1 candidates succeeded")
    if success_count < min_successful:
        raise CopilotCouncilError(f"Only {success_count} successful seats; minimum required is {min_successful}")
    if success_count == 1:
        stage2 = {
            "reviews": [],
            "review_failures": [],
            "review_substitutions": [],
            "aggregate": {
                "review_count": 0,
                "ranking": [stage1['candidates'][0]['label']],
                "by_answer": {},
                "unresolved_disagreements": [],
                "collective_blind_spots": [],
                "collective_blind_spot": "",
                "normalization_warnings": [],
            },
        }
        stage3 = {
            "ok": False,
            "model": cfg["chairman"]["model"],
            "transport": cfg["chairman"].get("transport", "copilot_api"),
            "endpoint": cfg["chairman"]["endpoint"],
            "final_answer": stage1["candidates"][0]["answer_text"],
            "raw_response": None,
            "attempts": [],
            "error": "Only one successful candidate; skipped review and chairman synthesis",
        }
    else:
        if mode == "peer":
            stage2 = run_stage2_peer(question, stage1, cfg)
        elif mode == "judge":
            stage2 = run_stage2_judge(question, stage1, cfg)
        elif mode == "collect":
            stage2 = {
                "reviews": [],
                "review_failures": [],
                "review_substitutions": [],
                "aggregate": {
                    "review_count": 0,
                    "ranking": sorted(stage1['labeled_answers'].keys()),
                    "by_answer": {},
                    "unresolved_disagreements": [],
                    "collective_blind_spots": [],
                    "collective_blind_spot": "",
                    "normalization_warnings": [],
                },
            }
        else:
            raise ValueError(f"Unsupported mode: {mode}")
        if mode == "collect":
            top_label = sorted(stage1["labeled_answers"].keys())[0]
            stage3 = {
                "ok": False,
                "model": cfg["chairman"]["model"],
                "transport": cfg["chairman"].get("transport", "copilot_api"),
                "endpoint": cfg["chairman"]["endpoint"],
                "final_answer": stage1["labeled_answers"][top_label],
                "raw_response": None,
                "attempts": [],
                "error": "Collect mode skips chairman synthesis",
            }
        else:
            stage3 = run_stage3_chairman(question, stage1, stage2, cfg, requested, resolved)
    result = {
        "timestamp": utc_now_iso(),
        "question": question,
        "mode": mode,
        "github_identity": get_active_github_identity(),
        "catalog_model_count": len(catalog_map),
        "requested_roster": requested,
        "resolved_roster": resolved,
        "roster": [{"seat_id": m.seat_id, "model": m.model, "transport": m.transport, "endpoint": m.endpoint, "role": m.role, "role_label": m.role_label} for m in roster],
        "chairman": cfg["chairman"],
        "summary_config": get_summary_config(cfg),
        "stage1": stage1,
        "stage2": stage2,
        "stage3": stage3,
        "failures": stage1.get("failures", []) + stage2.get("review_failures", []) + ([{"chairman_error": stage3.get("error"), "attempts": stage3.get("attempts", [])}] if stage3.get("error") else []),
    }
    result["compact_summary"] = render_summary_markdown(result)
    artifacts = write_artifacts(result, cfg["artifact_root"])
    result["artifacts"] = artifacts
    Path(artifacts["result_json"]).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result
