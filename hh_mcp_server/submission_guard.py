"""Allow only one HH application POST whose payload matches the reviewed draft."""
from email.parser import BytesParser
from email.policy import default
import json
from urllib.parse import parse_qs, urlparse


def parse_fields(body: bytes, content_type: str) -> dict[str, str]:
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate application field")
            result[name] = value
        return result

    if "application/json" in content_type:
        value = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or any(type(v) not in (str, int) for v in value.values()):
            raise ValueError("Application payload must contain scalar form fields")
        return {str(k): str(v) for k, v in value.items()}
    if "application/x-www-form-urlencoded" in content_type:
        value = parse_qs(body.decode(), keep_blank_values=True)
        if any(len(v) != 1 for v in value.values()):
            raise ValueError("Duplicate application form fields")
        return {k: v[0] for k, v in value.items()}
    if "multipart/form-data" in content_type:
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        result = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_filename() or name in result:
                raise ValueError("Unsupported or duplicate application field")
            result[name] = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        return result
    raise ValueError("Unsupported application encoding; no request sent")


def matches_application(fields: dict[str, str], expected: dict) -> bool:
    def same_text(actual: str | None, approved: str) -> bool:
        # Browser form serialization converts textarea LF to CRLF. Preserve
        # every other character, including leading/trailing spaces and lines.
        return actual is not None and actual.replace('\r\n', '\n') == approved.replace('\r\n', '\n')

    def one_of(names: tuple[str, ...], value: str) -> bool:
        found = [fields[name] for name in names if name in fields]
        return bool(found) and all(item == value for item in found)
    if not one_of(("vacancy_id", "vacancyId"), expected["vacancy_id"]):
        return False
    if not one_of(("resume_hash", "resume_id", "resumeId", "resumeHash"), expected["resume_id"]):
        return False
    letters = [fields[name] for name in ("letter", "cover_letter", "coverLetter") if name in fields]
    if (letters and any(not same_text(value, expected["cover_letter"]) for value in letters)) or (not letters and expected["cover_letter"]):
        return False
    answers = expected.get("question_answers") or {}
    if any(not same_text(fields.get(name), value) for name, value in answers.items()):
        return False
    # Unknown questionnaire answers must never be submitted automatically.
    if any(name.startswith("task_") and name not in answers for name in fields):
        return False
    return True


class SubmissionGuard:
    def __init__(self, expected: dict, on_dispatch):
        self.expected = expected
        self.on_dispatch = on_dispatch
        self.commit = False
        self.dispatched = False
        self.response_status = None
        self.error = None
        self.request = None

    async def handle(self, route):
        request = route.request
        target = urlparse(request.url)
        hostname = target.hostname or ""
        if target.scheme != "https" or not any(hostname == domain or hostname.endswith("." + domain) for domain in ("hh.ru", "hhcdn.ru")):
            await route.abort("blockedbyclient")
            return
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            await route.fallback()
            return
        is_application = target.hostname in {"hh.ru", "www.hh.ru"} and target.path.rstrip("/") in {
            "/applicant/vacancy_response/popup", "/applicant/vacancy_response",
        }
        if is_application:
            if not self.commit or self.dispatched or request.method != "POST":
                self.error = "A premature or duplicate application was blocked."
            else:
                try:
                    fields = parse_fields(request.post_data_buffer or b"", request.headers.get("content-type", ""))
                    if not matches_application(fields, self.expected):
                        raise ValueError("Payload differs from the approved resume, vacancy, letter or answers.")
                    self.on_dispatch()
                    self.dispatched = True
                    self.request = request
                    await route.fallback()
                    return
                except (ValueError, UnicodeError, KeyError, OSError):
                    self.error = "Application payload could not be verified; request blocked."
        await route.abort("blockedbyclient")

    def observe_response(self, response):
        if self.request is not None and response.request == self.request:
            self.response_status = response.status
