"""Opt-in TypeSafe/Jev evaluation. Standard library only; no gateway fallback."""

import json
import math
import os
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 1_048_576


class JevError(RuntimeError):
    """A safe-to-log failure: never includes credentials, state or response bodies."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _structured(value):
    return isinstance(value, (str, dict, list)) and bool(value)


def validate_questions(questions):
    """Reject malformed rubrics before any billable request."""
    if not isinstance(questions, dict) or not questions:
        raise JevError("questions must be a nonempty object")
    try:
        json.dumps(questions, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise JevError("questions must contain finite JSON values") from None
    for name, q in questions.items():
        if not isinstance(name, str) or not name or not isinstance(q, dict):
            raise JevError("each question needs a name and an object")
        if not _structured(q.get("instructions")):
            raise JevError("each question needs nonempty instructions")
        kind, criteria = q.get("type"), q.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                raise JevError("choice needs 2 to 255 criteria")
            if not all(isinstance(k, str) and k for k in criteria):
                raise JevError("choice criteria need nonempty string keys")
            if not all(v is None or isinstance(v, (str, dict, list)) for v in criteria.values()):
                raise JevError("invalid choice criterion")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise JevError("score needs 2 to 10 ordered criteria")
            if not all(isinstance(v, (str, dict, list)) for v in criteria):
                raise JevError("invalid score criterion")
        elif kind == "noul":
            if criteria is not None and (not isinstance(criteria, dict)
                                         or set(criteria) - {"true", "false"}
                                         or not all(isinstance(v, (str, dict, list)) for v in criteria.values())):
                raise JevError("noul criteria may only contain true and false")
        else:
            raise JevError("question type must be choice, noul or score")


def _number(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def _validate_response(body, questions):
    try:
        json.dumps(body, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise JevError("TypeSafe response contains invalid JSON values") from None
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"]:
        raise JevError("invalid TypeSafe response model")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("TypeSafe response does not match the requested questions")
    for name, q in questions.items():
        a = answers[name]
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise JevError("invalid TypeSafe answer type")
        if q["type"] == "noul":
            if not _number(a.get("noul"), 0, 1):
                raise JevError("invalid TypeSafe noul answer")
            continue
        keys = set(q["criteria"]) if q["type"] == "choice" else {str(i) for i in range(len(q["criteria"]))}
        probabilities = a.get("probabilities")
        if (not isinstance(probabilities, dict) or set(probabilities) != keys
                or not all(_number(p, 0, 1) for p in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.01)
                or not _number(a.get("confidence"), 0, 1)):
            raise JevError("invalid TypeSafe probability distribution")
        if q["type"] == "choice":
            if not isinstance(a.get("choice"), str) or a["choice"] not in keys:
                raise JevError("TypeSafe returned an unknown choice")
        elif not _number(a.get("score"), 0, len(keys) - 1):
            raise JevError("invalid TypeSafe score")
    usage = body.get("usage")
    if not isinstance(usage, dict) or not all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")
    ):
        raise JevError("invalid TypeSafe token usage")


class JevClient:
    """Direct API client. Explicit key or TYPESAFE_API_KEY; never reads Desktop files.

    One request per evaluation, no automatic retries. Callers control retry/backoff.
    Only submit data you are authorized to send to TypeSafe.
    """

    def __init__(self, api_key=None, model=DEFAULT_MODEL, timeout=30):
        key = os.environ.get("TYPESAFE_API_KEY") if api_key is None else api_key
        if not isinstance(key, str) or not key.strip():
            raise JevError("set TYPESAFE_API_KEY or pass an API key explicitly")
        key = key.strip()
        if any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in key):
            raise JevError("invalid TypeSafe API key format")
        if not isinstance(model, str) or not model.strip():
            raise JevError("model must be a nonempty string")
        if not _number(timeout, 0.001, 300):
            raise JevError("timeout must be between 0.001 and 300 seconds")
        self._api_key = key
        self.model = model
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def evaluate(self, state, questions):
        """Return validated answers, resolved model and token usage; no file/DB writes."""
        if not isinstance(state, (str, dict, list)):
            raise JevError("state must be text, an object or an array")
        validate_questions(questions)
        try:
            data = json.dumps({"model": self.model, "state": state, "questions": questions},
                              allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise JevError("request must contain finite JSON values") from None
        if len(data) > MAX_REQUEST_BYTES:
            raise JevError("TypeSafe request exceeds the 1 MiB client limit; reduce the input")
        if self._api_key.encode() in data:
            raise JevError("request contains the API credential; refusing to send")
        request = urllib.request.Request(ENDPOINT, data=data, headers={
            "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise JevError("unexpected TypeSafe response status")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise JevError(f"TypeSafe HTTP {code}; request not completed") from None
        except (urllib.error.URLError, OSError):
            raise JevError("TypeSafe connection failed or timed out") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise JevError("TypeSafe response exceeds the 1 MiB client limit")
        if self._api_key.encode() in raw:
            raise JevError("TypeSafe response contains a credential; refusing to expose it")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise JevError("TypeSafe returned invalid JSON") from None
        if self._api_key in json.dumps(body):
            raise JevError("TypeSafe response contains a credential; refusing to expose it")
        _validate_response(body, questions)
        # Ignore unrecognized top-level fields rather than propagating echoed input.
        return {k: body[k] for k in ("model", "answers", "usage")}

    def text_evaluator(self, questions):
        """Adapt UTF-8 fetched bytes to run_batch's optional evaluator hook."""
        validate_questions(questions)
        def evaluate_content(content):
            try:
                state = content.decode("utf-8")
            except UnicodeError:
                raise JevError("Jev sweep requires UTF-8 text; extract binary documents first") from None
            return self.evaluate(state, questions)
        return evaluate_content
