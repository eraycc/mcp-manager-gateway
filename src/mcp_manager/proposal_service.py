"""Transaction-local proposal transitions; downstream I/O belongs after commit."""
import json
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import delete, select

from .catalog_models import ProposalTestEntry
from .database import McpProposal, McpServer, now


async def approve_proposal(session, proposal_id, *, actor, expected_revision, catalog, values=None):
    # Import lazily to keep the workflow validation shared without a module cycle.
    from .proposals import validate_proposal_oauth

    row = await session.get(McpProposal, proposal_id, populate_existing=True)
    if not row:
        raise HTTPException(404, "Proposal not found")
    if row.status == "approved":
        return await session.get(McpServer, row.approved_mcp_id)
    if row.status == "rejected":
        raise HTTPException(409, "Rejected proposal cannot be approved")
    if expected_revision is not None and row.revision != expected_revision:
        raise HTTPException(409, "Proposal changed; reload before approval")
    values = values or {}
    payload = catalog.unseal(row.payload)
    mode = values.get("mode") or row.mode or "lazy"
    isolation = values.get("isolation") or row.isolation or "service"
    config_isolation = values.get("config_isolation") or row.config_isolation
    if config_isolation not in {None, "shared", "user"}:
        raise HTTPException(422, "Invalid OAuth config isolation")
    config = deepcopy(payload["config"])
    if config.get("auth", {}).get("type") == "oauth":
        validate_proposal_oauth(config["auth"])
        if config_isolation:
            config["auth"]["config_isolation"] = config_isolation
        else:
            config["auth"].pop("config_isolation", None)
    canonical = json.dumps(
        {"transport": payload["transport"], "config": config, "isolation": isolation},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    # This validation remains local to the transaction: no downstream connections.
    existing_rows = (await session.scalars(select(McpServer))).all()
    for existing in existing_rows:
        existing_config = await catalog.credentials.materialize(
            existing, actor.id, session=session, require=False
        )
        candidate = json.dumps(
            {"transport": existing.transport, "config": existing_config, "isolation": existing.isolation},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        if candidate == canonical:
            raise HTTPException(409, "An identical MCP service already exists")
    created = await catalog.create_in_session(
        session, {**payload, "config": config, "mode": mode, "isolation": isolation}, user_id=actor.id
    )
    row.status = "approved"
    row.mode, row.isolation, row.config_isolation = mode, isolation, config_isolation
    row.approved_mcp_id = created.id
    row.test_run_id = None
    await session.execute(delete(ProposalTestEntry).where(ProposalTestEntry.proposal_id == row.id))
    row.updated_at = now()
    await session.flush()
    return created


async def begin_test(session, proposal_id):
    from uuid import uuid4

    row = await session.get(McpProposal, proposal_id, populate_existing=True)
    if not row:
        raise HTTPException(404, "Proposal not found")
    if row.status in {"approved", "rejected"}:
        raise HTTPException(409, "Finalized proposal cannot be tested")
    run_id = str(uuid4())
    row.test_run_id = run_id
    row.tested_revision = None
    row.test_status = "pending"
    row.test_result = {}
    row.test_error = ""
    row.tested_at = None
    row.updated_at = now()
    await session.flush()
    return run_id, row.revision


async def finish_test(session, proposal_id, *, run_id, tested_revision, result=None, status, error=""):
    row = await session.get(McpProposal, proposal_id, populate_existing=True)
    if (
        not row or row.status in {"approved", "rejected"}
        or row.revision != tested_revision or row.test_run_id != run_id
    ):
        return False
    row.test_status = status
    row.tested_revision = tested_revision
    row.test_result = result or {}
    row.test_error = error[:4000]
    row.tested_at = now()
    row.updated_at = now()
    await session.flush()
    return True
