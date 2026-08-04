from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GoogleOmniError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleOmniSettings:
    project_id: str
    model: str = "gemini-omni-flash-preview"
    location: str = "global"
    endpoint_base: str = "https://aiplatform.googleapis.com/v1beta1"
    access_token_env: str = "GOOGLE_OAUTH_ACCESS_TOKEN"
    output_gcs_uri: str = ""
    poll_seconds: int = 15
    timeout_seconds: int = 900
    retries: int = 2

    @property
    def interactions_url(self) -> str:
        return (
            f"{self.endpoint_base}/projects/{self.project_id}"
            f"/locations/{self.location}/interactions"
        )


def generate_clip(
    *,
    prompt: str,
    output_path: Path,
    settings: GoogleOmniSettings,
    duration_seconds: int,
    aspect_ratio: str = "9:16",
    reference_images: list[str] | None = None,
    reference_videos: list[str] | None = None,
    background: bool = True,
    log_path: Path | None = None,
    extra_input: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate a single native-audio Omni Flash clip.

    The adapter accepts local media paths, gs:// URIs, or https:// URIs as references.
    Local files are sent as base64 content blocks; cloud/http references are sent as URIs.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not prompt.strip():
        raise GoogleOmniError("Google Omni clip generation requires a non-empty prompt.")
    if not (3 <= int(duration_seconds) <= 10):
        raise GoogleOmniError("Gemini Omni Flash clip duration must be between 3 and 10 seconds.")

    token = get_access_token(settings.access_token_env)
    prepared_reference_images = prepare_reference_images(
        reference_images or [],
        settings=settings,
        output_path=output_path,
        token=token,
    )
    started = time.monotonic()
    last_error: str | None = None
    interaction_id = ""
    response: dict[str, Any] = {}
    generation_prompt = prompt
    for generation_attempt in range(2):
        request_body = build_interaction_request(
            prompt=generation_prompt,
            settings=settings,
            duration_seconds=int(duration_seconds),
            aspect_ratio=aspect_ratio,
            reference_images=prepared_reference_images,
            reference_videos=reference_videos or [],
            background=background,
            extra_input=extra_input or [],
        )
        suffix = "" if generation_attempt == 0 else ".safety_retry"
        write_json(output_path.with_suffix(f"{suffix}.request.json"), redacted_request(request_body))
        response, submit_error = submit_interaction(settings, request_body, token=token)
        last_error = submit_error or last_error
        write_json(output_path.with_suffix(f"{suffix}.submit.json"), response)

        interaction_id = str(response.get("id") or "").strip()
        if not interaction_id:
            raise GoogleOmniError("Google Omni response did not include an interaction id.")
        response = poll_interaction(
            settings=settings,
            interaction_id=interaction_id,
            token=token,
            initial_response=response,
        )
        write_json(output_path.with_suffix(f"{suffix}.response.json"), response)
        failure_code = interaction_failure_code(response)
        if str(response.get("status") or "").lower() == "completed":
            break
        if failure_code != "responsible_ai_filtered" or generation_attempt > 0:
            raise GoogleOmniError(f"Google Omni interaction failed: {failure_code}.")
        generation_prompt = safety_retry_prompt(prompt)

    write_json(output_path.with_suffix(".response.json"), response)
    status = str(response.get("status") or "").lower()
    if status != "completed":
        raise GoogleOmniError(f"Google Omni interaction failed: {interaction_failure_code(response)}.")

    extracted = extract_video_output(response)
    if extracted.get("data"):
        output_path.write_bytes(base64.b64decode(str(extracted["data"])))
    elif extracted.get("uri"):
        materialize_video_uri(str(extracted["uri"]), output_path, token=token)
    else:
        raise GoogleOmniError(f"Google Omni interaction {interaction_id} did not return video data or URI.")

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise GoogleOmniError(f"Google Omni output was not saved: {output_path}")

    elapsed = round(time.monotonic() - started, 3)
    metadata = {
        "provider": "google_omni_flash",
        "model": settings.model,
        "interaction_id": interaction_id,
        "status": status,
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "output_path": str(output_path),
        "returned_uri": extracted.get("uri", ""),
        "generation_time_seconds": elapsed,
        "usage": response.get("usage", {}),
        "submit_error": last_error,
    }
    if log_path:
        append_jsonl(log_path, metadata)
    write_json(output_path.with_suffix(".metadata.json"), metadata)
    return metadata


def submit_interaction(
    settings: GoogleOmniSettings,
    request_body: dict[str, Any],
    *,
    token: str,
) -> tuple[dict[str, Any], str | None]:
    last_error: str | None = None
    for attempt in range(1, settings.retries + 2):
        try:
            return post_json(settings.interactions_url, request_body, token=token), last_error
        except Exception as exc:
            last_error = str(exc)
            if attempt > settings.retries:
                raise GoogleOmniError(f"Google Omni submit failed after {attempt} attempts: {exc}") from exc
            time.sleep(min(30, 2**attempt))
    raise GoogleOmniError("Google Omni submit failed without a response.")


def interaction_failure_code(response: dict[str, Any]) -> str:
    for step in response.get("steps", []) or []:
        if not isinstance(step, dict) or not isinstance(step.get("error"), dict):
            continue
        message = str(step["error"].get("message") or "").lower()
        if "responsible ai" in message or "filtered out" in message:
            return "responsible_ai_filtered"
        if "invalid argument" in message:
            return "invalid_argument"
        if "permission" in message or "denied" in message:
            return "permission_denied"
        if "quota" in message or "rate limit" in message:
            return "rate_limited"
        return "provider_generation_failed"
    status = str(response.get("status") or "").lower()
    return status if status in {"cancelled", "canceled", "expired"} else "provider_generation_failed"


def safety_retry_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "SAFETY RETRY: Treat this as a neutral culinary and consumer-education scene, not medical advice. "
        "Do not depict illness, bodily harm, treatment, diagnosis, or an extreme physical reaction. "
        "Keep the exact source frame, ordinary food handling, subtle natural motion, and calm conversational delivery."
    )


def build_interaction_request(
    *,
    prompt: str,
    settings: GoogleOmniSettings,
    duration_seconds: int,
    aspect_ratio: str,
    reference_images: list[str],
    reference_videos: list[str],
    background: bool,
    extra_input: list[dict[str, Any]],
) -> dict[str, Any]:
    timed_prompt = (
        f"{prompt.strip()}\n\n"
        f"Generate exactly {int(duration_seconds)} seconds of finished vertical video with native audio."
    )
    input_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": timed_prompt}
    ]
    for ref in reference_images:
        input_blocks.append(media_block(ref, expected_type="image"))
    for ref in reference_videos:
        input_blocks.append(media_block(ref, expected_type="video"))
    input_blocks.extend(extra_input)

    response_format: dict[str, Any] = {
        "type": "video",
        "aspect_ratio": aspect_ratio,
        "duration": f"{int(duration_seconds)}s",
    }
    if settings.output_gcs_uri:
        response_format["delivery"] = "uri"
        response_format["gcs_uri"] = settings.output_gcs_uri

    return {
        "model": settings.model,
        "input": input_blocks,
        "background": bool(background),
        "response_format": [response_format],
        "generation_config": {
            "video_config": {"task": "reference_to_video" if len(input_blocks) > 1 else "text_to_video"}
        },
    }


def media_block(reference: str, *, expected_type: str) -> dict[str, Any]:
    reference = str(reference).strip()
    if not reference:
        raise GoogleOmniError("Empty media reference in Google Omni clip config.")
    mime_type = guess_mime_type(reference, expected_type)
    if reference.startswith(("gs://", "http://", "https://")):
        return {"type": expected_type, "uri": reference, "mime_type": mime_type}
    path = Path(reference).expanduser().resolve()
    if not path.exists() or path.stat().st_size <= 0:
        raise GoogleOmniError(f"Media reference does not exist: {path}")
    return {
        "type": expected_type,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mime_type": mime_type,
    }


def prepare_reference_images(
    references: list[str],
    *,
    settings: GoogleOmniSettings,
    output_path: Path,
    token: str,
) -> list[str]:
    prepared: list[str] = []
    for reference in references:
        if str(reference).startswith(("gs://", "http://", "https://")):
            prepared.append(reference)
            continue
        if not settings.output_gcs_uri:
            raise GoogleOmniError(
                "Google Omni reference-to-video requires local reference images to be uploaded to Cloud Storage. "
                "Set google_omni_flash.output_gcs_uri or provide gs:// reference images."
            )
        path = Path(reference).expanduser().resolve()
        if not path.exists() or path.stat().st_size <= 0:
            raise GoogleOmniError(f"Media reference does not exist: {path}")
        bucket, prefix = parse_gcs_uri(settings.output_gcs_uri)
        safe_stem = "".join(char if char.isalnum() else "_" for char in path.stem).strip("_") or "reference"
        object_name = (
            f"{prefix.rstrip('/')}/inputs/{output_path.parent.name}/"
            f"{safe_stem}_{int(time.time())}{path.suffix.lower()}"
        ).lstrip("/")
        prepared.append(upload_gcs_file(path, bucket=bucket, object_name=object_name, token=token))
    return prepared


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise GoogleOmniError(f"Expected a gs:// URI, got: {uri}")
    remainder = uri[5:]
    bucket, _, prefix = remainder.partition("/")
    if not bucket:
        raise GoogleOmniError(f"Invalid GCS URI: {uri}")
    return bucket, prefix


def upload_gcs_file(path: Path, *, bucket: str, object_name: str, token: str) -> str:
    mime_type = guess_mime_type(str(path), "image")
    quoted_object = urllib.parse.quote(object_name, safe="")
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
        f"?uploadType=media&name={quoted_object}"
    )
    request = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": mime_type,
            "User-Agent": "ai-ugc-factory-google-omni-stack/1.0",
        },
    )
    try:
        with open_url(request, timeout=300) as response:
            json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleOmniError(f"Failed to upload {path} to gs://{bucket}/{object_name}: HTTP {exc.code}: {detail}") from exc
    return f"gs://{bucket}/{object_name}"


def guess_mime_type(reference: str, expected_type: str) -> str:
    guessed = mimetypes.guess_type(reference)[0]
    if guessed:
        return guessed
    if expected_type == "image":
        return "image/png"
    if expected_type == "video":
        return "video/mp4"
    return "application/octet-stream"


def get_access_token(env_name: str) -> str:
    token = os.environ.get(env_name, "").strip()
    if token:
        return token
    service_account_token = get_service_account_access_token()
    if service_account_token:
        return service_account_token
    metadata_request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        method="GET",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(metadata_request, timeout=5) as response:
            metadata_token = str(json.loads(response.read().decode("utf-8")).get("access_token") or "").strip()
            if metadata_token:
                return metadata_token
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        pass
    gcloud_cmd = r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    for command in (
        ["gcloud", "auth", "application-default", "print-access-token"],
        ["gcloud", "auth", "print-access-token"],
        [gcloud_cmd, "auth", "application-default", "print-access-token"],
        [gcloud_cmd, "auth", "print-access-token"],
    ):
        try:
            env = os.environ.copy()
            if os.environ.get("GOOGLE_OMNI_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
                env["CLOUDSDK_AUTH_DISABLE_SSL_VALIDATION"] = "True"
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False, env=env)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise GoogleOmniError(
        f"Missing Google auth token. Set {env_name}, GOOGLE_SERVICE_ACCOUNT_JSON, "
        "GOOGLE_APPLICATION_CREDENTIALS, or run `gcloud auth application-default login`."
    )


def get_service_account_access_token() -> str:
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw_json and not credentials_path:
        return ""
    try:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleOmniError(
            "GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_APPLICATION_CREDENTIALS requires google-auth and requests. "
            "Install them with `python -m pip install google-auth requests`."
        ) from exc

    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    try:
        if raw_json:
            try:
                info = json.loads(raw_json)
            except json.JSONDecodeError:
                info = json.loads(base64.b64decode(raw_json).decode("utf-8"))
            credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            credentials, _ = google.auth.default(scopes=scopes)
        credentials.refresh(Request())
    except Exception as exc:
        raise GoogleOmniError(f"Failed to mint Google Application Default Credentials access token: {exc}") from exc
    return str(credentials.token or "").strip()


def post_json(url: str, payload: dict[str, Any], *, token: str) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "ai-ugc-factory-google-omni-stack/1.0",
        },
    )
    try:
        with open_url(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleOmniError(f"HTTP {exc.code} from Google Omni API: {detail}") from exc


def post_empty(url: str, *, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ai-ugc-factory-google-omni-stack/1.0",
        },
    )
    try:
        with open_url(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleOmniError(f"HTTP {exc.code} from Google Omni API: {detail}") from exc


def get_json(url: str, *, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ai-ugc-factory-google-omni-stack/1.0",
        },
    )
    try:
        with open_url(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleOmniError(f"HTTP {exc.code} from Google Omni API: {detail}") from exc


def poll_interaction(
    *,
    settings: GoogleOmniSettings,
    interaction_id: str,
    token: str,
    initial_response: dict[str, Any],
) -> dict[str, Any]:
    response = initial_response
    deadline = time.monotonic() + settings.timeout_seconds
    while time.monotonic() < deadline:
        status = str(response.get("status") or "").lower()
        if status in {"completed", "failed", "cancelled", "canceled", "expired"}:
            return response
        time.sleep(settings.poll_seconds)
        response = get_json(f"{settings.interactions_url}/{interaction_id}", token=token)
    raise GoogleOmniError(
        f"Google Omni interaction {interaction_id} exceeded timeout of {settings.timeout_seconds}s."
    )


def extract_video_output(response: dict[str, Any]) -> dict[str, Any]:
    for step in response.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        for content in step.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "video":
                return {
                    "uri": content.get("uri") or content.get("gcsUri") or content.get("gcs_uri") or "",
                    "data": content.get("data") or "",
                    "mime_type": content.get("mime_type") or "video/mp4",
                }
    return {}


def materialize_video_uri(uri: str, output_path: Path, *, token: str) -> None:
    if uri.startswith("gs://"):
        download_gcs_file(uri, output_path, token=token)
        return
    if uri.startswith(("http://", "https://")):
        with open_url(uri, timeout=300) as response:
            output_path.write_bytes(response.read())
        return
    raise GoogleOmniError(f"Unsupported Google Omni output URI: {uri}")


def download_gcs_file(uri: str, output_path: Path, *, token: str) -> None:
    bucket, object_name = parse_gcs_uri(uri)
    quoted_object = urllib.parse.quote(object_name, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quoted_object}?alt=media"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "ai-ugc-factory-google-omni-stack/1.0",
        },
    )
    try:
        with open_url(request, timeout=600) as response:
            output_path.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GoogleOmniError(f"Failed to download {uri}: HTTP {exc.code}: {detail}") from exc


def open_url(request: urllib.request.Request | str, *, timeout: int):
    if os.environ.get("GOOGLE_OMNI_SSL_NO_VERIFY", "").strip().lower() in {"1", "true", "yes"}:
        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context)
    return urllib.request.urlopen(request, timeout=timeout)


def redacted_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(payload))
    for item in cleaned.get("input", []) or []:
        if isinstance(item, dict) and "data" in item:
            item["data"] = f"<base64:{len(str(item['data']))} chars>"
    return cleaned


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")
