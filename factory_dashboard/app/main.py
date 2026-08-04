from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .models import (
    AccountScheduleUpdate,
    ChatRequest,
    DraftRecord,
    DraftUpdate,
    GenerateRequest,
    JobRecord,
    new_id,
    utc_now,
)
from .services.accounts import AccountCatalog, AccountCatalogError
from .services.github_actions import GitHubActions, GitHubActionsError
from .services.openai_creative import CreativeServiceError, OpenAICreativeService, validate_source_config
from .store import build_store


settings = Settings.from_env()
store = build_store(settings)
catalog = AccountCatalog(settings, store)
creative = OpenAICreativeService(settings.openai_api_key, settings.openai_model)
github = GitHubActions(settings)

app = FastAPI(title="AI Content Factory Control Plane", version="0.1.0")
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


def require_admin(authorization: str | None = Header(default=None)) -> None:
    if not settings.admin_token:
        return
    if authorization != f"Bearer {settings.admin_token}":
        raise HTTPException(status_code=401, detail="Invalid dashboard token.")


@app.exception_handler(AccountCatalogError)
async def account_error(_: Request, exc: AccountCatalogError):
    return json_error(404, str(exc))


@app.exception_handler(CreativeServiceError)
async def creative_error(_: Request, exc: CreativeServiceError):
    return json_error(502, str(exc))


@app.exception_handler(GitHubActionsError)
async def github_error(_: Request, exc: GitHubActionsError):
    return json_error(502, str(exc))


def json_error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "factory-dashboard",
        "store": settings.store_backend,
        "openai_configured": bool(settings.openai_api_key),
        "github_configured": bool(settings.github_token),
    }


@app.get("/api/bootstrap", dependencies=[Depends(require_admin)])
def bootstrap(account_id: str | None = None) -> dict[str, Any]:
    accounts = catalog.list_accounts()
    selected = account_id or (accounts[0]["account_id"] if accounts else None)
    return {
        "accounts": accounts,
        "selected_account_id": selected,
        "drafts": store.list("drafts", account_id=selected)[:30] if selected else [],
        "jobs": refreshed_jobs(selected)[:40] if selected else [],
        "server_time": utc_now(),
    }


@app.get("/api/accounts", dependencies=[Depends(require_admin)])
def list_accounts() -> list[dict[str, Any]]:
    return catalog.list_accounts()


@app.get("/api/accounts/{account_id}", dependencies=[Depends(require_admin)])
def get_account(account_id: str) -> dict[str, Any]:
    return catalog.get_account(account_id)


@app.patch("/api/accounts/{account_id}/schedule", dependencies=[Depends(require_admin)])
def update_schedule(account_id: str, update: AccountScheduleUpdate) -> dict[str, Any]:
    return catalog.update_schedule(account_id, update.model_dump(exclude_none=True))


@app.post("/api/chat", dependencies=[Depends(require_admin)])
def chat(request: ChatRequest) -> dict[str, Any]:
    current = store.get("drafts", request.draft_id) if request.draft_id else None
    if current and current.get("account_id") != request.account_id:
        raise HTTPException(status_code=409, detail="Draft belongs to a different account.")
    context = catalog.generation_template(request.account_id)
    response = creative.create_or_revise(account_context=context, message=request.message, current_draft=current)
    now = utc_now()
    history = list((current or {}).get("chat_history") or [])
    history.extend(
        [
            {"role": "user", "content": request.message},
            {"role": "assistant", "content": str(response.get("assistant_message") or "Draft updated.")},
        ]
    )
    record = DraftRecord(
        id=str((current or {}).get("id") or new_id("draft")),
        account_id=request.account_id,
        title=str(response.get("title") or "Untitled concept")[:160],
        brief=str(response.get("brief") or request.message),
        caption=str(response.get("caption") or ""),
        status="draft",
        creative_spec=dict(response["source_config"]),
        chat_history=history,
        version=int((current or {}).get("version") or 0) + 1,
        created_at=str((current or {}).get("created_at") or now),
        updated_at=now,
    ).model_dump()
    store.put("drafts", record["id"], record)
    return {"assistant_message": response.get("assistant_message"), "draft": record}


@app.get("/api/drafts", dependencies=[Depends(require_admin)])
def list_drafts(account_id: str | None = None) -> list[dict[str, Any]]:
    return store.list("drafts", account_id=account_id)


@app.get("/api/drafts/{draft_id}", dependencies=[Depends(require_admin)])
def get_draft(draft_id: str) -> dict[str, Any]:
    draft = store.get("drafts", draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return draft


@app.patch("/api/drafts/{draft_id}", dependencies=[Depends(require_admin)])
def update_draft(draft_id: str, update: DraftUpdate) -> dict[str, Any]:
    draft = store.get("drafts", draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    values = update.model_dump(exclude_none=True)
    if "creative_spec" in values:
        values["creative_spec"]["account_id"] = draft["account_id"]
        validate_source_config(values["creative_spec"])
    draft.update(values)
    draft["version"] = int(draft.get("version") or 0) + 1
    draft["updated_at"] = utc_now()
    return store.put("drafts", draft_id, draft)


@app.post("/api/drafts/{draft_id}/generate", dependencies=[Depends(require_admin)])
def generate(draft_id: str, request: GenerateRequest) -> dict[str, Any]:
    draft = store.get("drafts", draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return queue_draft(draft, request)


@app.get("/api/jobs", dependencies=[Depends(require_admin)])
def list_jobs(account_id: str | None = None) -> list[dict[str, Any]]:
    return refreshed_jobs(account_id)


@app.post("/api/system/tick")
def scheduler_tick(x_factory_cron: str | None = Header(default=None)) -> dict[str, Any]:
    cron_token = __import__("os").environ.get("DASHBOARD_CRON_TOKEN", "").strip()
    if cron_token and x_factory_cron != cron_token:
        raise HTTPException(status_code=401, detail="Invalid scheduler token.")
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for account in catalog.due_accounts():
        deterministic_id = f"scheduled_{account['account_id']}_{account['local_date']}"
        if store.get("jobs", deterministic_id):
            skipped.append({"account_id": account["account_id"], "reason": "already_queued"})
            continue
        concept = catalog.select_due_concept(account, account["local_date"])
        context = catalog.generation_template(account["account_id"], concept["concept_id"])
        now = utc_now()
        draft = DraftRecord(
            id=f"draft_{deterministic_id}",
            account_id=account["account_id"],
            title=str(context["source_config"].get("name") or concept["concept_id"]),
            brief="Autopilot generation from the account's approved V3 concept rotation.",
            caption=str(concept.get("caption") or ""),
            status="approved",
            creative_spec=context["source_config"],
            version=1,
            created_at=now,
            updated_at=now,
        ).model_dump()
        store.put("drafts", draft["id"], draft)
        publish_at = datetime.fromisoformat(
            f"{account['local_date']}T{account['publish_time']}:00"
        ).replace(tzinfo=ZoneInfo(account["timezone"])).isoformat()
        queued.append(queue_draft(draft, GenerateRequest(publish_at=publish_at, dry_run=False), job_id=deterministic_id))
    return {"status": "ok", "queued": queued, "skipped": skipped, "checked_at": utc_now()}


def queue_draft(draft: dict[str, Any], request: GenerateRequest, *, job_id: str | None = None) -> dict[str, Any]:
    account_id = str(draft["account_id"])
    catalog.get_account(account_id)
    job_id = job_id or new_id("job")
    now = utc_now()
    concept_id = str(draft["creative_spec"].get("concept_id") or job_id)
    payload = {
        "schema_version": 1,
        "request_id": job_id,
        "account_id": account_id,
        "concept_id": concept_id,
        "caption": draft.get("caption") or "",
        "source_config": draft["creative_spec"],
    }
    job = JobRecord(
        id=job_id,
        account_id=account_id,
        draft_id=str(draft["id"]),
        concept_id=concept_id,
        publish_at=request.publish_at,
        dry_run=request.dry_run,
        skip_publish=request.skip_publish,
        created_at=now,
        updated_at=now,
    ).model_dump()
    store.put("jobs", job_id, job)
    github.dispatch(
        request_id=job_id,
        account_id=account_id,
        payload=payload,
        publish_at=request.publish_at,
        dry_run=request.dry_run,
        skip_publish=request.skip_publish,
    )
    return job


def refreshed_jobs(account_id: str | None) -> list[dict[str, Any]]:
    jobs = store.list("jobs", account_id=account_id)
    if not settings.github_token:
        return jobs
    for job in jobs[:20]:
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            continue
        update = github.find_run(job["id"])
        if update:
            job.update(update)
            store.put("jobs", job["id"], job)
    return jobs


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/{path:path}")
def spa_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(static_dir / "index.html")
