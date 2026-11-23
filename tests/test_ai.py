"""The AI layer, with the network replaced: every kind of service is asked correctly and its answer is read
correctly. Nothing leaves this machine and no key is needed."""
import json
import os
import sys
import tempfile

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-ai-")
os.environ["DATABASE_URL"] = ""
os.environ.update(PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="model-a, model-b", PEAKCUT_AI_KEY="test-key-123456", PEAKCUT_AI_BASE_URL="")
sys.path.insert(0, ".")
from studio import ai, auth, brain, config, ctx, db, usage  # noqa: E402

db.init()
fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    ok or fails.append(name)


class Reply:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


CLIP = {"start": 1, "end": 20, "score": 80, "hook": "WOW", "title": "t", "description": "d", "hashtags": ["a"], "reason": "r"}
seen = []


def fake_post(url, json=None, headers=None, timeout=None, **_k):  # noqa: A002
    seen.append((url, json, headers or {}))
    if url.endswith(":generateContent"):
        return Reply(200, {"candidates": [{"content": {"parts": [{"text": globals()["json_text"]([CLIP])}]}}],
                           "usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 130}})
    if url.endswith("/chat/completions"):
        if "temperature" in json:                      # a model that refuses an extra setting: asked again without it
            return Reply(400, {"error": {"message": "temperature is not supported"}})
        return Reply(200, {"choices": [{"message": {"content": "```json\n" + globals()["json_text"]({"items": [CLIP]}) + "\n```"}}],
                           "usage": {"prompt_tokens": 90, "completion_tokens": 20}})
    if url.endswith("/messages"):
        return Reply(200, {"content": [{"type": "text", "text": "Here you go: " + globals()["json_text"]({"items": [CLIP]})}],
                           "usage": {"input_tokens": 70, "output_tokens": 10}})
    return Reply(404, {})


def json_text(x):
    return json.dumps(x)


ai.httpx.post = fake_post


def use(provider, base=""):
    config.AI_PROVIDER, config.AI_BASE_URL = provider, base
    seen.clear()


use("gemini")
out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
check("gemini: answer and tokens", out == [CLIP] and (sent, wrote) == (100, 30), str((out, sent, wrote)))
check("gemini: key in the header, schema passed as is", seen[0][2].get("x-goog-api-key") == "test-key-123456"
      and seen[0][1]["generationConfig"]["responseSchema"] is brain.SCHEMA and seen[0][0].endswith("/model-a:generateContent"))

for name, host in (("openai", "api.openai.com"), ("xai", "api.x.ai"), ("grok", "api.x.ai"), ("groq", "api.groq.com"), ("openrouter", "openrouter.ai")):
    use(name)
    out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
    check(f"{name}: answer unwrapped from a code fence, tokens read", out == [CLIP] and (sent, wrote) == (90, 20), str((out, sent, wrote)))
    check(f"{name}: right address, key as a bearer token", host in seen[0][0] and seen[0][2].get("Authorization") == "Bearer test-key-123456")
check("a refused extra setting is dropped and asked again", len(seen) == 2 and "temperature" not in seen[1][1])
check("the schema is told in the prompt for chat models", '"type": "array"' in seen[1][1]["messages"][0]["content"])

use("anthropic")
out, sent, wrote = ai.call("model-a", "hello", brain.SCHEMA)
check("anthropic: answer found inside a sentence, tokens read", out == [CLIP] and (sent, wrote) == (70, 10), str((out, sent, wrote)))
check("anthropic: key and version headers", seen[0][2].get("x-api-key") == "test-key-123456" and "anthropic-version" in seen[0][2])

use("custom", "http://127.0.0.1:1234/v1")
out, _s, _w = ai.call("model-a", "hello", brain.SCHEMA)
check("custom: uses the address from settings", seen[0][0] == "http://127.0.0.1:1234/v1/chat/completions" and out == [CLIP])

use("custom")
check("custom without an address is not ready", not ai.ready())
use("nonsense")
check("an unknown provider is not ready", not ai.ready())
use("ollama")
os.environ["PEAKCUT_AI_KEY"] = ""
config._kept.clear()
check("ollama needs no key", ai.ready() and ai.label().startswith("Ollama"))
out, _s, _w = ai.call("model-a", "hello", brain.SCHEMA)
check("ollama: no key header is sent", "Authorization" not in seen[0][2] and out == [CLIP])
use("gemini")
check("gemini without a key is not ready", not ai.ready())
os.environ["PEAKCUT_AI_KEY"] = "test-key-123456"
config._kept.clear()

# ---- the whole path: a call is made for a user and shows up in their usage, and only theirs
u1, _ = auth._create("ai-one@example.com", "One", password_hash="x")
u2, _ = auth._create("ai-two@example.com", "Two", password_hash="x")
words = [{"w": f"word{i}", "s": i * 0.5, "e": i * 0.5 + 0.4} for i in range(120)]
with ctx.as_user(u1.id):
    usage.set_context(None, "A test video")
    import numpy as np
    clips, info = brain.find_clips(words, np.zeros(60), 60.0, title="T", uploader="U")
    check("clips come back through the whole path", len(clips) == 1 and clips[0]["hook"] == "WOW" and info["model"] == "model-a", str((clips, info)))
    s = usage.summary({})
    check("the call is in this user's usage", s["calls_total"] == 1 and s["tokens_total"] == 130 and s["recent"][0]["where"] == "A test video", str(s))
    check("usage says which AI is set", s["ready"] and s["provider"] == "Gemini" and s["model"] == "model-a")
with ctx.as_user(u2.id):
    check("another user sees none of it", usage.summary({})["calls_total"] == 0)

# ---- Gemini: Find models -> Save and test with a discovered model
GEM = "https://generativelanguage.googleapis.com/v1beta/models"
LISTING = {"models": [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-2.5-flash-image-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/aqa", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
    {"name": "models/imagen-4.0-generate-001", "supportedGenerationMethods": ["predict"]}]}
use("gemini")
real_get, real_post_fn = ai.httpx.get, ai.httpx.post
ai.httpx.get = lambda url, **k: Reply(200, LISTING)
found = ai.list_models("gemini", with_key="test-key-123456")
check("gemini discovery lists only text generation models, bare ids",
      found == ["gemini-2.5-flash", "gemma-3-27b-it"], str(found))
ai.save("gemini", found[0], "", "test-key-123456")
seen.clear()
ai.httpx.post = fake_post
res = ai.test()
check("save and test with a discovered gemini model reaches the same /v1beta/models/<id> address",
      res["ok"] and seen[0][0] == f"{GEM}/gemini-2.5-flash:generateContent", str((res, seen[:1])))
ai.save("gemini", "models/gemini-2.5-flash", "", None)
seen.clear()
ai.test()
check("a models/ prefix is not doubled in the address", seen[0][0] == f"{GEM}/gemini-2.5-flash:generateContent", seen[0][0])

# Discovered models that reject responseSchema (e.g. Gemma) fall back to prompt JSON
schema_calls = []
def fake_gemma_post(url, json=None, headers=None, timeout=None):
    schema_calls.append(json)
    if "generationConfig" in (json or {}):
        return Reply(400, {"error": {"code": 400, "message": "JSON mode is not enabled for models/gemma-3-27b-it"}})
    return Reply(200, {"candidates": [{"content": {"parts": [{"text": '{"items": ["ok"]}'}]}}],
                       "usageMetadata": {"promptTokenCount": 8, "totalTokenCount": 12}})
ai.save("gemini", "gemma-3-27b-it", "", None)
ai.httpx.post = fake_gemma_post
gemma_res = ai.test()
check("discovered gemini model without responseSchema support succeeds via prompt JSON fallback",
      gemma_res["ok"] and len(schema_calls) == 2, str((gemma_res, len(schema_calls))))

def explained(text):
    return ai.explain(ai.AIError(text))
check("a real unknown model (404) still names the model",
      "doesn't know the model" in explained("HTTP 404 models/gemini-old is not found for API version v1beta, or is not supported for generateContent"))
check("a retired or new-user-restricted model (404) says it is retired rather than unknown",
      "retired" in explained("HTTP 404 This model models/gemini-2.0-flash is no longer available to new users. Please update your code to use a newer model."))
check("a 400 'API key not found' is a key problem, not an unknown model",
      "refused the key" in explained('HTTP 400 {"error": {"code": 400, "message": "API key not found. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}'))
check("a stray 404 inside another error is not an unknown model",
      "doesn't know" not in explained('HTTP 400 {"error": {"message": "Invalid JSON payload at contents[404]", "status": "INVALID_ARGUMENT"}}'))
check("a 400 about the request itself is not called an unknown model",
      "doesn't know" not in explained('HTTP 400 {"error": {"message": "Invalid value at generation_config.response_schema", "status": "INVALID_ARGUMENT"}}'))
check("a 404 that is not about a model is not called an unknown model",
      "doesn't know" not in explained('HTTP 404 {"error": {"message": "Method not allowed for this path"}}'))
check("quota is still quota", "limit" in explained('HTTP 429 {"error": {"status": "RESOURCE_EXHAUSTED"}}'))
ai.httpx.get, ai.httpx.post = real_get, real_post_fn

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil  # noqa: E402
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
