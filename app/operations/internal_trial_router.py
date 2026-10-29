"""Private administrator preview for isolated source observations."""
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, select, func

from app.db import get_db
from app.operations.router import require_admin
from app.operations.tasks import TaskError, form_token, verify_token
from app.operations import internal_trial as trial
from app.operations import internal_trial_workflows as workflow

router = APIRouter(prefix="/operations/internal-test", dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory="app/templates")
MAX_UPLOAD = 512 * 1024


def schema_ready(engine):
    inspector = inspect(engine)
    names = set(inspector.get_table_names())
    expected = set(trial.metadata.tables)
    if not expected <= names:
        return False
    for table in trial.metadata.sorted_tables:
        if {c["name"] for c in inspector.get_columns(table.name)} != set(table.c.keys()):
            raise HTTPException(409, "Existing test tables need schema review; no changes made")
    return True


def _batch_id(value):
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise HTTPException(422, "Invalid batch ID")
    return value


def _verify(token, actor, purpose):
    try:
        verify_token(token, actor, 0, purpose=purpose)
    except TaskError as exc:
        raise HTTPException(403, str(exc)) from None


def _render(request, actor, engine, **extra):
    ready = schema_ready(engine)
    batch_list = []
    if ready:
        with engine.connect() as conn:
            for row in conn.execute(select(trial.batches.c.id, trial.batches.c.state,
                                           trial.batches.c.manifest).limit(100)).mappings():
                observations = json.loads(row["manifest"])
                batch_list.append({"id": row["id"], "state": row["state"],
                    "counts": {kind: sum(r["kind"] == kind for r in observations) for kind in trial.FIELDS}})
    context = dict(ready=ready, batches=batch_list,
                   initialize_token=form_token(actor, purpose="trial-initialize"),
                   upload_token=form_token(actor, purpose="trial-preview"), **extra)
    return templates.TemplateResponse(request=request, name="internal_trial.html", context=context,
                                      headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


@router.get("")
def index(request: Request, q: str = "", actor=Depends(require_admin), db=Depends(get_db)):
    return _workspace(request, actor, db.get_bind(), q=q)


@router.get("/imports")
def imports(request: Request, actor=Depends(require_admin), db=Depends(get_db)):
    return _render(request, actor, db.get_bind())


def _workspace(request, actor, engine, *, q="", scenario_id=None):
    try:
        cards = workflow.listing_cards(engine)
        workflow_ready = workflow.ready(engine)
        scenario_rows = workflow.read(engine) if workflow_ready else []
    except workflow.SimulationError as exc:
        raise HTTPException(409, str(exc)) from None
    total = len(cards)
    query = q.strip()[:100].casefold()
    cards = [card for card in cards if not query or query in
             " ".join(str(v) for v in card["payload"].values()).casefold()]
    for card in cards:
        card["create_token"] = form_token(actor, purpose="trial-scenario-create:" +
                                         card["key"] + ":" + card["fingerprint"])
    selected = None
    if scenario_id:
        selected = next((row for row in scenario_rows if row["id"] == scenario_id), None)
        if not selected:
            raise HTTPException(404, "Active simulation not found")
        selected["actions"] = [{"name": action, "token": form_token(actor,
            SimpleNamespace(id=selected["id"], version=selected["revision"]),
            purpose="trial-scenario-action:" + action)}
            for action in workflow.available_actions(selected["data"])]
        selected["history"] = workflow.history(engine, scenario_id)
    today = date.today()
    return templates.TemplateResponse(request=request, name="internal_trial_workspace.html",
        context={"cards": cards, "source_count": total, "q": q.strip()[:100],
            "workflow_ready": workflow_ready, "prepare_token": form_token(actor, purpose="trial-prepare-workflows"),
            "simulations": scenario_rows, "selected_scenario": selected,
            "default_start": today.isoformat(), "default_end": (today+timedelta(days=2)).isoformat(),
            "calendar": workflow.calendar(engine, cards, today) if workflow_ready else None},
        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


@router.post("/prepare-workflows")
def prepare_workflows(token: str = Form(...), actor=Depends(require_admin), db=Depends(get_db)):
    _verify(token, actor, "trial-prepare-workflows")
    if not schema_ready(db.get_bind()):
        raise HTTPException(409, "Prepare and import source listings first")
    try:
        workflow.prepare(db.get_bind())
    except workflow.SimulationError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/operations/internal-test", status_code=303)


@router.post("/simulations")
def create_simulation(source_key: str = Form(...), fingerprint: str = Form(...),
                      check_in: str = Form(...), check_out: str = Form(...),
                      nightly_rate: str = Form(...), token: str = Form(...),
                      actor=Depends(require_admin), db=Depends(get_db)):
    _batch_id(source_key); _batch_id(fingerprint)
    _verify(token, actor, "trial-scenario-create:" + source_key + ":" + fingerprint)
    try:
        if not workflow.ready(db.get_bind()):
            raise workflow.SimulationError("Prepare the isolated workflow area first")
        scenario_id = workflow.create(db.get_bind(), source_key=source_key, fingerprint=fingerprint,
            check_in=check_in, check_out=check_out, nightly_rate=nightly_rate,
            command_digest=workflow.digest(token), actor=actor)
    except workflow.SimulationError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/operations/internal-test/simulations/" + scenario_id, status_code=303)


@router.get("/simulations/{scenario_id}")
def simulation_page(request: Request, scenario_id: str, actor=Depends(require_admin), db=Depends(get_db)):
    if not re.fullmatch(r"[0-9a-f]{32}", scenario_id):
        raise HTTPException(404, "Simulation not found")
    return _workspace(request, actor, db.get_bind(), scenario_id=scenario_id)


@router.post("/simulations/{scenario_id}/{action}")
def simulation_action(scenario_id: str, action: str, token: str = Form(...),
                      actor=Depends(require_admin), db=Depends(get_db)):
    if not re.fullmatch(r"[0-9a-f]{32}", scenario_id) or action not in workflow.ACTIONS:
        raise HTTPException(422, "Invalid simulation or action")
    try:
        revision = verify_token(token, actor, scenario_id, purpose="trial-scenario-action:" + action)
    except TaskError as exc:
        raise HTTPException(403, str(exc)) from None
    try:
        if not workflow.ready(db.get_bind()):
            raise workflow.SimulationError("Prepare the isolated workflow area first")
        workflow.apply_action(db.get_bind(), scenario_id=scenario_id, action=action, revision=revision,
                              command_digest=workflow.digest(token), actor=actor)
    except workflow.SimulationError as exc:
        raise HTTPException(409, str(exc)) from None
    path = "/operations/internal-test" if action == "archive" else "/operations/internal-test/simulations/" + scenario_id
    return RedirectResponse(path, status_code=303)


@router.get("/status")
def status(db=Depends(get_db)):
    from app.models import Property, Booking, CleaningTask, IcalFeed, MessageLog, Cleaner
    live = {model.__tablename__: db.query(model).count() for model in
            (Property, Booking, CleaningTask, IcalFeed, MessageLog, Cleaner)}
    live["published_properties"] = db.query(Property).filter(Property.published.is_(True)).count()
    ready = schema_ready(db.get_bind())
    counts = {kind: 0 for kind in trial.FIELDS}
    active_batches = 0
    if ready:
        with db.get_bind().connect() as conn:
            for manifest in conn.execute(select(trial.batches.c.manifest)
                    .where(trial.batches.c.state == "quarantined")).scalars():
                active_batches += 1
                for row in json.loads(manifest):
                    counts[row["kind"]] += 1
    try:
        workflow_ready = workflow.ready(db.get_bind())
    except workflow.SimulationError as exc:
        raise HTTPException(409, str(exc)) from None
    synthetic_count = 0
    if workflow_ready:
        with db.get_bind().connect() as conn:
            synthetic_count = conn.execute(select(func.count()).select_from(workflow.scenarios)
                .where(workflow.scenarios.c.archived.is_(False))).scalar_one()
    return {"schema_ready": ready, "workflow_schema_ready": workflow_ready,
            "active_synthetic_scenarios": synthetic_count, "active_batches": active_batches,
            "source_observations": counts, "physical_units_reconciled": False,
            "trial_started": False, "live_counts": live}


@router.post("/initialize")
def initialize(token: str = Form(...), actor=Depends(require_admin), db=Depends(get_db)):
    _verify(token, actor, "trial-initialize")
    engine = db.get_bind()
    names = set(inspect(engine).get_table_names())
    present = names & set(trial.metadata.tables)
    if present:
        if not schema_ready(engine):
            raise HTTPException(409, "Partial test schema requires review; no changes made")
    else:
        trial.install_schema(engine)
    return RedirectResponse("/operations/internal-test", status_code=303)


@router.post("/preview")
async def preview(request: Request, file: UploadFile, token: str = Form(...),
                  actor=Depends(require_admin), db=Depends(get_db)):
    _verify(token, actor, "trial-preview")
    content = await file.read(MAX_UPLOAD + 1)
    await file.close()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "Use at most 512 KiB per source snapshot")
    try:
        plan = trial.preview(content)
    except trial.TrialImportError as exc:
        raise HTTPException(422, str(exc)) from None
    return _render(request, actor, db.get_bind(), preview=plan,
                   apply_token=form_token(actor, purpose="trial-apply:" + plan["id"]))


@router.post("/apply")
def apply(manifest: str = Form(...), digest: str = Form(...), token: str = Form(...),
          actor=Depends(require_admin), db=Depends(get_db)):
    digest = _batch_id(digest)
    _verify(token, actor, "trial-apply:" + digest)
    if not schema_ready(db.get_bind()):
        raise HTTPException(409, "Initialize the isolated test tables first")
    content = manifest.encode()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "Use at most 512 KiB per source snapshot")
    try:
        trial.apply(db.get_bind(), content, expected_digest=digest)
    except trial.TrialImportError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/operations/internal-test/" + digest, status_code=303)


@router.get("/{batch_id}")
def detail(request: Request, batch_id: str, actor=Depends(require_admin), db=Depends(get_db)):
    batch_id = _batch_id(batch_id)
    if not schema_ready(db.get_bind()):
        raise HTTPException(404, "Test snapshot not found")
    observations = trial.observations(db.get_bind(), batch_id)
    return _render(request, actor, db.get_bind(), selected_id=batch_id,
                   observations=observations, rollback_token=form_token(actor, purpose="trial-rollback:" + batch_id))


@router.post("/{batch_id}/rollback")
def rollback(batch_id: str, token: str = Form(...), note: str = Form(...),
             actor=Depends(require_admin), db=Depends(get_db)):
    batch_id = _batch_id(batch_id)
    _verify(token, actor, "trial-rollback:" + batch_id)
    if not schema_ready(db.get_bind()):
        raise HTTPException(404, "Test snapshot not found")
    try:
        trial.rollback(db.get_bind(), batch_id, note=note)
    except trial.TrialImportError as exc:
        raise HTTPException(422, str(exc)) from None
    return RedirectResponse("/operations/internal-test", status_code=303)

