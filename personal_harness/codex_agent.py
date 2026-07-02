"""File protocol for using the current Codex agent as the AEGIS LLM generator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Set
import json
from uuid import uuid4

from .aegis import AdaptationLandscape
from .core import HarnessConfig
from .eval import CandidateManifest
from .evolution import CandidateEdit

REQUEST_SCHEMA_VERSION = "codex-agent-candidate-request/v1"
RESPONSE_SCHEMA_VERSION = "codex-agent-candidate-response/v1"
LLM_OWNER = "current-codex-agent"
CANDIDATE_DIR = Path(".harness") / "candidates"
REQUEST_FILE = CANDIDATE_DIR / "codex-candidate-request.json"
RESPONSE_FILE = CANDIDATE_DIR / "codex-candidate-response.json"


@dataclass(frozen=True)
class CodexCandidateRequest:
    current_version: str
    target_tasks: Set[str]
    edit_buckets: Set[str]
    failure_categories: Mapping[str, str]
    request_id: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "llm_owner": LLM_OWNER,
            "current_version": self.current_version,
            "target_tasks": sorted(self.target_tasks),
            "edit_buckets": sorted(self.edit_buckets),
            "failure_categories": dict(sorted(self.failure_categories.items())),
            "instructions_zh_hant": [
                "你就是目前的 Codex agent；不要呼叫外部 LLM API。",
                "請根據 target_tasks 與 failure_categories 產生候選 harness edit。",
                "回覆寫入 codex-candidate-response.json，schema_version 必須是 codex-agent-candidate-response/v1。",
                "第一版只允許宣告 target_version 與 manifest；真正程式修改由 Codex agent 在 repo 中另行完成。",
            ],
        }
        if self.request_id is not None:
            payload["request_id"] = self.request_id
            payload["instructions_zh_hant"].append("回覆必須原樣包含 request_id，避免舊 response 被新的 request 誤用。")
        return payload


@dataclass(frozen=True)
class CodexCandidateResponse:
    name: str
    target_version: str
    summary: str
    expected_improvements: Set[str]
    expected_regressions: Set[str]
    smoke_ok: bool = True
    request_id: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "llm_owner": LLM_OWNER,
            "name": self.name,
            "target_version": self.target_version,
            "summary": self.summary,
            "expected_improvements": sorted(self.expected_improvements),
            "expected_regressions": sorted(self.expected_regressions),
            "smoke_ok": self.smoke_ok,
        }
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodexCandidateResponse":
        if data.get("schema_version") != RESPONSE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported Codex candidate response schema: {data.get('schema_version')}")
        if data.get("llm_owner") != LLM_OWNER:
            raise ValueError(f"Unsupported llm_owner: {data.get('llm_owner')}")
        return cls(
            name=str(data["name"]),
            target_version=str(data["target_version"]),
            summary=str(data["summary"]),
            expected_improvements=set(map(str, data.get("expected_improvements", []))),
            expected_regressions=set(map(str, data.get("expected_regressions", []))),
            smoke_ok=bool(data.get("smoke_ok", True)),
            request_id=str(data["request_id"]) if "request_id" in data else None,
        )


class CodexAgentEvolver:
    """Bridges AEGIS Evolver to the current Codex agent via JSON files.

    This deliberately does not call an external LLM. The active Codex agent reads
    the request file, edits code or docs if needed, and writes a response file under the standalone .harness runtime.
    The response is then converted into deterministic CandidateEdit objects.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def request_path(self) -> Path:
        return self.root / REQUEST_FILE

    @property
    def response_path(self) -> Path:
        return self.root / RESPONSE_FILE

    def write_request(self, request: CodexCandidateRequest) -> Path:
        if request.request_id is None:
            request = replace(request, request_id=f"codex-agent:{uuid4().hex}")
        self.request_path.parent.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(json.dumps(request.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return self.request_path

    def request_from_landscape(self, current: HarnessConfig, landscape: AdaptationLandscape) -> Path:
        return self.write_request(
            CodexCandidateRequest(
                current_version=current.version,
                target_tasks=set(landscape.target_tasks),
                edit_buckets=set(landscape.edit_buckets),
                failure_categories=dict(landscape.failure_categories),
            )
        )

    def read_response_candidates(self, request_id: str | None = None, *, allow_uncorrelated: bool = False) -> List[CandidateEdit]:
        if request_id is None and not allow_uncorrelated:
            raise ValueError("Codex candidate response consumption requires request_id")
        if not self.response_path.exists():
            return []
        raw = json.loads(self.response_path.read_text(encoding="utf-8"))
        responses = raw if isinstance(raw, list) else [raw]
        candidates: List[CandidateEdit] = []
        for item in responses:
            if request_id is not None and item.get("request_id") != request_id:
                continue
            response = CodexCandidateResponse.from_dict(item)
            candidates.append(
                CandidateEdit(
                    name=response.name,
                    apply=lambda harness, target_version=response.target_version: harness.with_version(target_version),
                    manifest=CandidateManifest(
                        summary=response.summary,
                        expected_improvements=set(response.expected_improvements),
                        expected_regressions=set(response.expected_regressions),
                    ),
                    smoke_ok=response.smoke_ok,
                )
            )
        return candidates


__all__ = [
    "CANDIDATE_DIR",
    "LLM_OWNER",
    "REQUEST_FILE",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_FILE",
    "RESPONSE_SCHEMA_VERSION",
    "CodexAgentEvolver",
    "CodexCandidateRequest",
    "CodexCandidateResponse",
]
