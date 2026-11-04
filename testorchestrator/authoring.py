"""Native Katalon authoring — round-trip .ts edits + draft/publish.

Edits real source files, never a reconstruction from discovery
inventory. Unknown nodes/attrs/comments/namespaces pass through;
unsupported structure → read-only, never a destructive rewrite."""

import json
from pathlib import Path

from flask import current_app
from xml.etree import ElementTree as etree

from . import git_backend
from .execution import AuthoringDraft, DiscoveredItem, GitBinding
from .extensions import db


def _authoring_wt(binding_id: str) -> Path:
    return Path(current_app.instance_path) / "authoring" / binding_id


def _clone_for_authoring(binding: GitBinding) -> Path:
    """Separate authoring checkout — never the fetch or report-pub
    worktree."""
    wt = _authoring_wt(binding.id)
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        git_backend.run("clone", binding.url, wt,
                        branch=binding.authoring_branch or binding.ref,
                        auth=None)
    return wt


def parse_ts(path: Path) -> dict:
    """Round-trip parse a .ts file — returns kind, name, members,
    dynamic flag, supported flag, and a reason for read-only."""
    try:
        # preserve comments — default stdlib parser drops them.
        parser = etree.XMLParser(
            target=etree.TreeBuilder(insert_comments=True))
        tree = etree.parse(str(path), parser=parser)
    except Exception as exc:
        return {"kind": "", "supported": False,
                "reason": f"unparseable XML: {exc}"}
    root = tree.getroot()
    tag = root.tag.split('}')[-1]
    if tag == "TestSuiteEntity":
        members = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue                    # comment/PI nodes
            if el.tag.split('}')[-1] == "testCaseId":
                members.append(el.text or "")
        dyn = any(isinstance(e.tag, str) and
                  e.tag.split('}')[-1] == "testCaseRunType"
                  and (e.text or "").lower() in ("dynamic", "data")
                  for e in root.iter())
        name_el = next((e for e in root.iter()
                        if isinstance(e.tag, str) and
                        e.tag.split('}')[-1] == "name"), None)
        return {"kind": "suite", "supported": not dyn,
                "dynamic": dyn,
                "reason": ("dynamic suite — query membership is not "
                           "editable" if dyn else ""),
                "name": (name_el.text if name_el is not None
                         else path.stem),
                "members": members, "_tree": tree}
    if tag == "TestSuiteCollectionEntity":
        members = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag.split('}')[-1] == "testSuiteEntity":
                members.append(el.text or "")
        return {"kind": "collection", "supported": True,
                "name": path.stem, "members": members, "_tree": tree}
    return {"kind": tag, "supported": False,
            "reason": f"unsupported root <{tag}>"}


def write_ts_members(path: Path, parsed: dict, members: list) -> None:
    """Replace ONLY the member list in the parsed tree — other nodes,
    attrs, comments, namespaces pass through untouched."""
    tree = parsed["_tree"]
    root = tree.getroot()
    kind = parsed["kind"]
    # find each member element in order and update text, or remove if
    # absent from new list (bounded edit — don't rebuild the doc)
    member_tag = ("testCaseId" if kind == "suite"
                  else "testSuiteEntity")
    parent_tag = ("testCaseLink" if kind == "suite"
                  else "TestSuiteRunConfiguration")
    parents = [e for e in root.iter()
               if isinstance(e.tag, str) and
               e.tag.split('}')[-1] == parent_tag]
    # parent-of-member map (stdlib has no getparent())
    pmap = {c: p for p in root.iter() for c in p}
    for i, m in enumerate(members):
        if i < len(parents):
            p = parents[i]
            for e in p.iter():
                if isinstance(e.tag, str) and \
                    e.tag.split('}')[-1] == member_tag:
                    e.text = m
    # extra parents beyond new length → remove via the map
    for p in parents[len(members):]:
        pmap[p].remove(p)
    # new members beyond existing → clone the last parent into its
    # container (root for suites, runConfigurations for collections)
    if len(members) > len(parents) and parents:
        import copy
        container = pmap[parents[-1]]
        for m in members[len(parents):]:
            np = copy.deepcopy(parents[-1])
            for e in np.iter():
                if isinstance(e.tag, str) and \
                    e.tag.split('}')[-1] == member_tag:
                    e.text = m
            container.append(np)
    tree.write(str(path), xml_declaration=True,
               encoding="UTF-8")


def open_draft(username: str, item: DiscoveredItem) -> AuthoringDraft:
    """Open a user-owned draft of a discovered suite/collection —
    isolated per owner; base revision recorded."""
    d = AuthoringDraft(binding_id=item.binding_id,
                       project_id=item.project_id, owner=username,
                       kind=item.kind, path=item.path,
                       base_sha=item.sha, name=item.name,
                       members_json=json.dumps(item.members() or []))
    db.session.add(d)
    db.session.commit()
    return d


def draft_diff(draft: AuthoringDraft) -> str:
    """Changed members diff for review — approval binds to this."""
    wt = _authoring_wt(draft.binding_id)
    f = wt / draft.path
    parsed = parse_ts(f) if f.exists() else {"members": []}
    old = parsed.get("members", [])
    new = draft.members()
    lines = []
    for m in old:
        if m not in new:
            lines.append(f"- {m}")
    for m in new:
        if m not in old:
            lines.append(f"+ {m}")
    return "\n".join(lines) or "(no membership changes)"


def draft_signature(draft: AuthoringDraft) -> str:
    """Review-bound identity: draft content + artifact + destination +
    base revision. Any change invalidates a prior review."""
    import hashlib
    h = hashlib.sha256()
    h.update(draft.members_json.encode())
    h.update(draft.path.encode())
    h.update(draft.base_sha.encode())
    binding = db.session.get(GitBinding, draft.binding_id)
    h.update((binding.url if binding else "").encode())
    h.update(((binding.authoring_branch or binding.ref)
              if binding else "").encode())
    return h.hexdigest()[:24]


def publish_draft(draft: AuthoringDraft,
                  reviewed_sig: str = "") -> dict:
    """Apply the draft's members to the real file, commit+push to the
    authoring branch — upstream conflict blocks visibly."""
    binding = db.session.get(GitBinding, draft.binding_id)
    # stale-review check: the caller's reviewed signature must match
    # the CURRENT draft+destination+base — changed content, a moved
    # destination, or an outdated preview is rejected, never
    # silently published.
    if reviewed_sig and reviewed_sig != draft_signature(draft):
        return {"error": "reviewed preview is stale — review again"}
    wt = _clone_for_authoring(binding)
    branch = binding.authoring_branch or binding.ref or "main"
    # refresh remote state — local HEAD alone is stale and misses
    # upstream commits (Studio edits) made after the draft opened.
    try:
        git_backend.run("update", wt, branch)
    except Exception as exc:
        return {"error": f"cannot refresh remote: {str(exc)[:200]}"}
    head = git_backend.run("resolve", wt, "HEAD")
    if draft.base_sha and head != draft.base_sha:
        return {"error": ("upstream advanced since the draft's base "
                          "revision — resolve before publish"),
                "head": head, "base": draft.base_sha}
    f = wt / draft.path
    if not f.exists():
        return {"error": "source file missing"}
    parsed = parse_ts(f)
    if not parsed.get("supported"):
        return {"error": parsed.get("reason", "unsupported")}
    write_ts_members(f, parsed, draft.members())
    sha = git_backend.run("commit_all", wt,
                          f"authoring: {draft.path}")
    try:
        git_backend.run("push", wt, branch)
    except Exception as exc:
        return {"error": str(exc)[:200]}
    draft.status = "published"
    draft.published_sha = sha
    db.session.commit()
    return {"published": True, "sha": sha}


def cleanup_abandoned(owner: str = "") -> int:
    """Remove abandoned drafts + the authoring workspaces — audit
    events already recorded; nothing active or foreign is touched."""
    import shutil
    q = AuthoringDraft.query.filter_by(status="abandoned")
    if owner:
        q = q.filter_by(owner=owner)
    rows = q.all()
    n = 0
    for d in rows:
        wt = _authoring_wt(d.binding_id)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        db.session.delete(d)
        n += 1
    db.session.commit()
    return n
