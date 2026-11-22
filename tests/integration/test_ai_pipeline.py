"""AI-assisted paths (profile, extraction, matching) against a mocked Gemini endpoint."""

from __future__ import annotations

import asyncio
import json

import httpx

from src.ai.provider_manager import AIManager
from src.core.models import SearchConfig
from src.core.orchestrator import Orchestrator, build_http
from src.cv.parser import parse_cv
from src.cv.profile_builder import ProfileBuilder
from tests.conftest import fixture_json, public_resolver, with_credentials
from tests.test_pipeline import _router, _settings

CV_ANSWER = {
    "current_title": "Fullstack Developer", "experience_years": 6.5, "seniority_estimate": "senior",
    "skills": [{"name": "TypeScript", "category": "programming_language", "evidence": "React + TypeScript dashboards"},
               {"name": "Kafka", "category": "tool", "evidence": "event streaming with Kafka"}],
    "target_positions": [{"title": "Senior Fullstack Engineer", "directness": "direct", "rationale": "x"},
                         {"title": "API Developer", "directness": "adjacent", "rationale": "REST APIs"}],
    "certifications": ["AWS Certified Cloud Practitioner (2023)"], "summary": "Fullstack developer.",
}


def gemini_handler(seen: list[str]):
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json=fixture_json("gemini_models.json"))
        body = json.loads(req.content)
        prompt = body["contents"][0]["parts"][0]["text"]
        if "Analyse the CV" in prompt:
            seen.append("cv")
            assert "andi.pratama@example.com" not in prompt and "[email removed]" in prompt  # redacted
            answer = CV_ANSWER
        elif "For each item" in prompt:
            seen.append("extract")
            answer = {"items": [{"id": "0", "kind": "social_post", "is_job_opportunity": True,
                                 "company_name": "Fixture Labs", "job_title": "Backend Developer",
                                 "location": "Atlantis HQ"}]}  # 'Atlantis HQ' is not in the text -> must be rejected
        elif "Assess each job" in prompt:
            seen.append("match")
            answer = {"items": [{"id": "0", "role_equivalence": 0.9, "seniority_fit": "appropriate",
                                 "explanation": "Strong backend overlap.", "concerns": ["Confirm remote policy"]}]}
        else:
            answer = {}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]},
                                                         "finishReason": "STOP"}]})
    return handle


def test_profile_builder_with_ai(settings, router, sample_cv_path) -> None:
    seen: list[str] = []
    router.add("generativelanguage.googleapis.com", "/", gemini_handler(seen))
    s = with_credentials(settings, gemini_api_key="k")

    async def go():
        http = build_http(s, transport=router.transport(), resolver=public_resolver)
        try:
            return await ProfileBuilder(s, AIManager.from_settings(s, http)).build(parse_cv(sample_cv_path), use_ai=True)
        finally:
            await http.aclose()

    p = asyncio.run(go())
    assert seen == ["cv"]
    assert p.analysis_method == "deterministic+ai"
    assert "Kafka" not in p.skills and "Kafka" in p.transferable_skills  # unsupported by CV text
    assert p.seniority_estimate in ("mid",)  # AI 'senior' never raises the deterministic estimate
    assert "Fullstack Engineer" in p.target_positions and "API Developer" in p.adjacent_positions


def test_pipeline_with_ai_assistance(settings, sample_cv_path) -> None:
    seen: list[str] = []
    s = with_credentials(_settings(settings), gemini_api_key="k")
    router = _router()
    router.add("generativelanguage.googleapis.com", "/", gemini_handler(seen))

    async def go():
        http = build_http(s, transport=router.transport(), resolver=public_resolver)
        try:
            profile = await ProfileBuilder(s, AIManager.from_settings(s, http)).build(parse_cv(sample_cv_path), use_ai=True)
        finally:
            await http.aclose()
        orch = Orchestrator(s, transport=router.transport(), resolver=public_resolver)
        try:
            return await orch.run(profile, SearchConfig(publication_window_days=None))
        finally:
            orch.close()

    result = asyncio.run(go())
    assert {"cv", "extract", "match"} <= set(seen)
    social = [j for j in result.data.jobs if "linkedin.com/posts" in j.source_url][0]
    assert social.company_name == "Fixture Labs"  # supported by the snippet
    assert social.location != "Atlantis HQ"  # hallucinated value rejected
    assert any(j.match.ai_assessment for j in result.data.jobs)
    assert "gemini" in result.data.ai_status
