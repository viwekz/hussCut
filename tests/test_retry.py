"""Free AI keys are limited per minute. A "too many requests" answer is waited out long enough to pass, the wait the
service asks for is followed, a limit for the whole day is not waited out, and nothing is waited after the last try.

Runs against the code directly, with its own empty data folder."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-retry-")
sys.path.insert(0, ".")
from unittest.mock import patch
from studio import ai, brain, config
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


def failure(status, text="", headers=None):
    try:
        ai._fail(Resp(status, text, headers), "m")
    except ai.AIError as e:
        return e


# ---- what the service says is read
check("a Retry-After header is read", failure(429, "slow down", {"retry-after": "17"}).wait, 17.0)
check("Gemini's retryDelay is read", failure(429, '{"error":{"details":[{"retryDelay": "33s"}]}}').wait, 33.0)
check("no wait is invented when none is given", failure(429, "slow down").wait, None)
long_body = '{"error":{"message":"' + "x" * 400 + '","quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}'
check("a daily limit is noticed even deep in a long answer", failure(429, long_body).daily, True)
check("a per-minute limit is not taken for a daily one", failure(429, '{"quotaId":"PerMinute"}').daily, False)
check("a bad request carries no wait", failure(400, "bad", {"retry-after": "5"}).wait, None)

# ---- how long is waited
rate = failure(429, "slow down")
check("the first wait is long enough for a per-minute limit", brain.retry_wait(rate, 0), 20.0)
check("the second wait is longer", brain.retry_wait(rate, 1), 40.0)
check("nothing is waited after the last try", brain.retry_wait(rate, brain.TRIES - 1), None)
check("the wait the service asks for is followed", brain.retry_wait(failure(429, "x", {"retry-after": "7"}), 0), 7.0)
check("a very long asked wait is capped", brain.retry_wait(failure(429, "x", {"retry-after": "900"}), 0), float(brain.MAX_WAIT))
check("a live check never waits long", brain.retry_wait(rate, 0, timeout=45), float(brain.LIVE_MAX_WAIT))
check("a daily limit is not waited out", brain.retry_wait(failure(429, long_body), 0), None)
check("an overloaded service is given a short wait", brain.retry_wait(failure(503, "overloaded"), 0), 5.0)
check("a refused key is not retried", brain.retry_wait(failure(401, "bad key"), 0), None)

# ---- the whole retry loop
sleeps = []
answers = [failure(429, "slow down"), failure(429, "slow down"), ([{"hook": "h"}], 10, 5)]


def fake_call(model, prompt, schema, timeout=None):
    a = answers.pop(0)
    if isinstance(a, Exception):
        raise a
    return a


with patch.object(config, "ai_ready", return_value=True), patch.object(ai, "models", return_value=["m1"]), \
        patch.object(ai, "call", side_effect=fake_call), patch.object(brain.time, "sleep", side_effect=sleeps.append):
    out, model, used = brain.ask_ai("p")
check("a limited key gets an answer on the third try", out, [{"hook": "h"}])
check("it waited 20 then 40 seconds", sleeps, [20.0, 40.0])

sleeps.clear()
tried = []


def daily_then_ok(model, prompt, schema, timeout=None):
    tried.append(model)
    if model == "m1":
        raise failure(429, long_body)
    return [{"hook": "h"}], 1, 1


with patch.object(config, "ai_ready", return_value=True), patch.object(ai, "models", return_value=["m1", "m2"]), \
        patch.object(ai, "call", side_effect=daily_then_ok), patch.object(brain.time, "sleep", side_effect=sleeps.append):
    out, model, used = brain.ask_ai("p")
check("a model out of its daily limit goes straight to the next one", (tried, model, sleeps), (["m1", "m2"], "m2", []))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
