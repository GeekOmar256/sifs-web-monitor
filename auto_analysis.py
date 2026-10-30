"""Analyze saved plant photos on the Pi without exposing the API key to browsers."""

import json
import logging
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

log = logging.getLogger("sifs-analysis")

PROMPT = (
    "Assess this plant photo for a home grower. Identify the likely crop and growth "
    "stage, and describe only visible health concerns. Do not claim a disease "
    "diagnosis from an image alone. If uncertain, say so. Keep recommendations brief."
)

FIELDS = {
    "crop_type": {"type": "string"},
    "growth_stage": {"type": "string"},
    "health_status": {"type": "string"},
    "visible_issues": {"type": "array", "items": {"type": "string"}},
    "recommendations": {"type": "array", "items": {"type": "string"}},
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
}


def analyze_photo(image, api_key, model="gpt-4.1-mini", opener=urlopen):
    body = {
        "model": model,
        "store": False,
        "max_output_tokens": 700,
        "text": {"format": {"type": "json_schema", "name": "plant_assessment",
                            "strict": True, "schema": {"type": "object",
                                                       "properties": FIELDS,
                                                       "required": list(FIELDS),
                                                       "additionalProperties": False}}},
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": PROMPT},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image}",
             "detail": "high"},
        ]}],
    }
    request = Request("https://api.openai.com/v1/responses",
                      data=json.dumps(body).encode("utf-8"), method="POST",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})
    try:
        with opener(request, timeout=45) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"OpenAI request failed (HTTP {exc.code})") from exc
    output = "".join(part.get("text", "") for item in result.get("output", [])
                     for part in item.get("content", []) if part.get("type") == "output_text")
    if not output:
        raise ValueError("OpenAI returned no assessment text")
    assessment = json.loads(output)
    if not isinstance(assessment, dict) or set(assessment) != set(FIELDS):
        raise ValueError("OpenAI returned an incomplete assessment")
    if not all(isinstance(assessment[key], str) for key in
               ("crop_type", "growth_stage", "health_status", "confidence")):
        raise ValueError("OpenAI returned invalid assessment fields")
    if not all(isinstance(assessment[key], list) and
               all(isinstance(value, str) for value in assessment[key])
               for key in ("visible_issues", "recommendations")):
        raise ValueError("OpenAI returned invalid assessment lists")
    return assessment


def run_auto_analysis(state, stop_event):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("SIFS_OPENAI_MODEL", "gpt-4.1-mini").strip()
    while not stop_event.is_set():
        job = state.next_analysis_job()
        if not job:
            stop_event.wait(2)
            continue
        photo_id, image, captured_at = job
        if not api_key:
            state.set_analysis_status("needs_key", "OpenAI API key is not configured on the Pi")
            stop_event.wait(30)
            continue
        state.set_analysis_status("analyzing", None)
        try:
            result = analyze_photo(image, api_key, model)
            state.complete_analysis(photo_id, {**result, "photoCapturedAt": captured_at})
            log.info("Analyzed photo captured at %s", captured_at)
        except Exception as exc:
            delay = state.defer_analysis(photo_id, str(exc))
            log.warning("Photo analysis failed; retrying in %s seconds: %s", delay, exc)
            stop_event.wait(min(delay, 30))
