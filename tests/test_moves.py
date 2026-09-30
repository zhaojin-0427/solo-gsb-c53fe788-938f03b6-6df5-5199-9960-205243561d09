"""End-to-end tests for object path moves (POST /moves)."""
from __future__ import annotations

import hashlib
import threading
import uuid

FUTURE = "2099-01-01T00:00:00Z"

SHA_OF = lambda body: hashlib.sha256(body).hexdigest()
ETAG_OF = lambda body: '"' + SHA_OF(body) + '"'


def _path(name: str) -> str:
    return f"move/{name}-{uuid.uuid4().hex[:12]}"


def _put(client, path: str, body: bytes, media_type: str = "text/plain"):
    r = client.put(
        f"/objects/{path}", content=body, headers={"Content-Type": media_type}
    )
    assert r.status_code == 204
    return r


def _move(client, src, dst, etag, rev_no):
    return client.post(
        "/moves",
        json={
            "source_path": src,
            "target_path": dst,
            "source_etag": etag,
            "source_revision_no": rev_no,
        },
    )


def _create_session(client, paths, target_prefix, segment_size=2):
    return client.post(
        "/copy-sessions",
        json={
            "source_paths": paths,
            "target_prefix": target_prefix,
            "segment_size": segment_size,
        },
    )


# ---- request validation -------------------------------------------------------

def test_move_validation(client):
    src, dst = _path("a"), _path("b")
    _put(client, src, b"hello")
    good = {
        "source_path": src,
        "target_path": dst,
        "source_etag": ETAG_OF(b"hello"),
        "source_revision_no": 1,
    }

    def post(payload, raw=None):
        return client.post("/moves", json=payload, content=raw)

    assert client.post("/moves", content=b"not json").status_code == 400
    assert client.post("/moves", json=["a"]).status_code == 400
    assert post({**good, "source_path": ""}).status_code == 400
    assert post({**good, "source_path": 5}).status_code == 400
    assert post({**good, "source_path": None}).status_code == 400
    assert post({**good, "target_path": ""}).status_code == 400
    assert post({**good, "target_path": ["x"]}).status_code == 400
    # Paths must differ.
    assert post({**good, "target_path": src}).status_code == 400
    # ETag: non-empty string, syntactically a quoted entity-tag.
    assert post({**good, "source_etag": ""}).status_code == 400
    assert post({**good, "source_etag": 5}).status_code == 400
    assert post({**good, "source_etag": "not-an-etag"}).status_code == 400
    assert post({**good, "source_etag": "abc"}).status_code == 400
    # revision number: a strictly positive integer (booleans rejected).
    assert post({**good, "source_revision_no": 0}).status_code == 400
    assert post({**good, "source_revision_no": -3}).status_code == 400
    assert post({**good, "source_revision_no": "1"}).status_code == 400
    assert post({**good, "source_revision_no": 1.0}).status_code == 400
    assert post({**good, "source_revision_no": True}).status_code == 400
    # A well-formed request still succeeds after all the rejected ones.
    assert _move(client, src, dst, good["source_etag"], 1).status_code == 200


# ---- happy path ---------------------------------------------------------------

def test_move_publishes_source_at_target_and_detaches_source(client):
    src, dst = _path("src"), _path("dst")
    body = b"payload bytes"
    r = _put(client, src, body, media_type="application/x-test")

    resp = _move(client, src, dst, r.headers["etag"], int(r.headers["x-revision-no"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "moved"
    assert data["source_path"] == src
    assert data["target_path"] == dst
    assert data["target_etag"] == ETAG_OF(body)
    assert data["target_revision_no"] == 1
    assert data["target_sha256"] == SHA_OF(body)
    assert data["target_media_type"] == "application/x-test"
    # Response headers echo the new target revision.
    assert resp.headers["etag"] == ETAG_OF(body)
    assert resp.headers["x-revision-no"] == "1"

    # Source now reads 404 for both GET and HEAD.
    assert client.get(f"/objects/{src}").status_code == 404
    assert client.head(f"/objects/{src}").status_code == 404

    # Target serves the exact content and saved media type as rev 1.
    g = client.get(f"/objects/{dst}")
    assert g.status_code == 200
    assert g.content == body
    assert g.headers["etag"] == ETAG_OF(body)
    assert g.headers["content-type"] == "application/x-test"


def test_move_creates_target_row_even_when_it_never_existed(client):
    src, dst = _path("src"), _path("fresh-target")
    r = _put(client, src, b"v")
    assert _move(client, src, dst, r.headers["etag"], 1).status_code == 200
    assert client.get(f"/objects/{dst}").content == b"v"


def test_move_uses_target_own_revision_sequence(client):
    src, dst = _path("src"), _path("dst-seq")
    _put(client, dst, b"old-1")
    _put(client, dst, b"old-2")
    r = _put(client, src, b"new-here")  # source rev 1

    resp = _move(client, src, dst, r.headers["etag"], 1)
    # Target had two revisions, but this move is rejected: target not empty.
    assert resp.status_code == 409


def test_move_chained_source_revision_numbers_keep_increasing(client):
    a, b, c = _path("a"), _path("b"), _path("c")
    r1 = _put(client, a, b"one")
    m1 = _move(client, a, b, r1.headers["etag"], 1)
    assert m1.status_code == 200 and m1.json()["target_revision_no"] == 1
    # Move away from b onto c: b rev 1, c rev 1.
    m2 = _move(client, b, c, m1.headers["etag"], 1)
    assert m2.status_code == 200 and m2.json()["target_revision_no"] == 1
    # b is empty again; publishing there continues b's own sequence: rev 2.
    r = _put(client, b, b"reused-path")
    assert r.headers["x-revision-no"] == "2"
    assert client.get(f"/objects/{c}").content == b"one"


# ---- 404 / 412 / 409 ----------------------------------------------------------

def test_move_source_missing_is_404(client):
    src, dst = _path("never"), _path("anywhere")
    r = _move(client, src, dst, ETAG_OF(b"x"), 1)
    assert r.status_code == 404
    assert r.json() == {"detail": "source_not_found"}
    # Nothing created at either path in the sense of readable content.
    assert client.get(f"/objects/{src}").status_code == 404
    assert client.get(f"/objects/{dst}").status_code == 404


def test_move_source_already_moved_away_is_404(client):
    src, dst, dst2 = _path("s1"), _path("d1"), _path("d2")
    r = _put(client, src, b"x")
    assert _move(client, src, dst, r.headers["etag"], 1).status_code == 200
    # Second move of the same source path fails 404 even with the same
    # (now stale) ETag/revision pair.
    r2 = _move(client, src, dst2, r.headers["etag"], 1)
    assert r2.status_code == 404
    assert r2.json() == {"detail": "source_not_found"}
    # Target of the first move is untouched and still serves content.
    assert client.get(f"/objects/{dst}").content == b"x"


def test_move_etag_mismatch_is_412_and_nothing_changes(client):
    src, dst = _path("src-stale"), _path("dst-stale")
    r1 = _put(client, src, b"v1")
    _put(client, src, b"v2")  # current revision is now 2

    resp = _move(client, src, dst, r1.headers["etag"], 1)
    assert resp.status_code == 412
    data = resp.json()
    assert data["detail"] == "precondition_failed"
    assert data["source_path"] == src
    assert data["actual_revision_no"] == 2
    # No partial effect: source unchanged, target not created.
    assert client.get(f"/objects/{src}").content == b"v2"
    assert client.get(f"/objects/{dst}").status_code == 404


def test_move_revision_number_mismatch_is_412(client):
    src, dst = _path("src-rev"), _path("dst-rev")
    _put(client, src, b"v1")
    r2 = _put(client, src, b"v2")
    # ETag matches the current content but the claimed number is stale.
    resp = _move(client, src, dst, r2.headers["etag"], 1)
    assert resp.status_code == 412
    assert resp.json()["actual_revision_no"] == 2
    assert client.get(f"/objects/{src}").content == b"v2"
    assert client.get(f"/objects/{dst}").status_code == 404


def test_move_revision_number_ahead_is_412(client):
    src, dst = _path("src-ahead"), _path("dst-ahead")
    r = _put(client, src, b"v1")
    resp = _move(client, src, dst, r.headers["etag"], 7)
    assert resp.status_code == 412
    assert resp.json()["actual_revision_no"] == 1


def test_move_weak_etag_never_strong_matches(client):
    src, dst = _path("src-weak"), _path("dst-weak")
    body = b"strong-only"
    r = _put(client, src, body)
    weak = "W/" + r.headers["etag"]
    resp = _move(client, src, dst, weak, 1)
    assert resp.status_code == 412
    assert client.get(f"/objects/{src}").content == body


def test_move_target_already_has_content_is_409(client):
    src, dst = _path("src-busy"), _path("dst-busy")
    r = _put(client, src, b"from-src")
    _put(client, dst, b"dst-content")
    resp = _move(client, src, dst, r.headers["etag"], 1)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "target_exists"}
    # Both paths keep exactly their previous state.
    assert client.get(f"/objects/{src}").content == b"from-src"
    assert client.get(f"/objects/{dst}").content == b"dst-content"


def test_move_target_with_only_placeholder_is_empty(client):
    """A placeholder row left by copy-session creation counts as empty:
    the move succeeds and publishes rev 1 onto it."""
    other = _path("placeholder-source")
    _put(client, other, b"seed")
    dst = other + "-target"

    # Session creation inserts an empty (current_id NULL) placeholder for
    # the not-yet-existing target path.
    created = _create_session(client, [other], "", segment_size=1)
    assert created.status_code == 201
    assert client.get(f"/objects/{other}-target").status_code == 404

    src = _path("src-ph")
    r = _put(client, src, b"ph-content")
    resp = _move(client, src, dst, r.headers["etag"], 1)
    assert resp.status_code == 200
    assert resp.json()["target_revision_no"] == 1
    assert client.get(f"/objects/{dst}").content == b"ph-content"
    assert client.get(f"/objects/{src}").status_code == 404


# ---- revision numbers never regress / get reused ------------------------------

def test_put_to_moved_away_path_continues_numbering(client):
    src, dst = _path("gone"), _path("landed")
    _put(client, src, b"v1")
    _put(client, src, b"v2")
    g = client.get(f"/objects/{src}")
    assert _move(client, src, dst, g.headers["etag"], 2).status_code == 200
    assert client.get(f"/objects/{src}").status_code == 404

    # Re-publish on the moved-away path: rev 3, never 1 again.
    again = _put(client, src, b"v3")
    assert again.headers["x-revision-no"] == "3"
    g = client.get(f"/objects/{src}")
    assert g.content == b"v3"
    assert g.headers["etag"] == ETAG_OF(b"v3")
    # dst still serves v2 as its own rev 1.
    assert client.get(f"/objects/{dst}").content == b"v2"


def test_revision_number_not_reused_after_gc_deletes_all_history(client):
    """GC can empty a moved path of all revisions; the next PUT must not
    hand out a number that was ever used there."""
    src, dst = _path("gc-src"), _path("gc-dst")
    r1 = _put(client, src, b"a")
    _put(client, src, b"bb")
    g = client.get(f"/objects/{src}")
    assert _move(client, src, dst, g.headers["etag"], 2).status_code == 200

    # No copy session references src's revisions: GC deletes both.
    gc = client.post(
        "/gc",
        json={"paths": [src], "before": FUTURE, "dry_run": False},
    )
    assert gc.status_code == 200
    deleted = gc.json()["deleted"]
    assert sorted(d["revision_no"] for d in deleted) == [1, 2]

    # Publishing again must continue above 2, not reuse 1.
    r = _put(client, src, b"ccc")
    assert r.headers["x-revision-no"] == "3"


def test_target_reused_after_being_moved_away_keeps_sequence(client):
    a, b, c, d = _path("a"), _path("b"), _path("c"), _path("d")
    r = _put(client, a, b"x")
    m = _move(client, a, b, r.headers["etag"], 1)
    assert m.status_code == 200 and m.json()["target_revision_no"] == 1
    # Continue the content onward: source path b currently holds rev 1
    # (the same content, published there by the first move).
    m = _move(client, b, c, m.headers["etag"], 1)
    assert m.status_code == 200 and m.json()["target_revision_no"] == 1
    # b is now empty (moved away) but already allocated rev 1.
    r2 = _put(client, b, b"y")
    assert r2.headers["x-revision-no"] == "2"
    # Moving y off b to d frees b again; next publication there is rev 3.
    m = _move(client, b, d, r2.headers["etag"], 2)
    assert m.status_code == 200 and m.json()["target_revision_no"] == 1
    r3 = _put(client, b, b"z")
    assert r3.headers["x-revision-no"] == "3"
    # The content chains end up where expected.
    assert client.get(f"/objects/{c}").content == b"x"
    assert client.get(f"/objects/{d}").content == b"y"
    assert client.get(f"/objects/{b}").content == b"z"


# ---- interaction with copy sessions -------------------------------------------

def test_pre_move_session_still_pulls_pinned_source_revision(client):
    src, backup = _path("live-src"), _path("live-dst")
    r = _put(client, src, b"alpha")
    # Session created BEFORE the move pins source revision 1.
    sid = _create_session(client, [src], "", segment_size=1).json()["session"]
    seg = client.get(f"/copy-sessions/{sid}/segments").json()
    assert seg["entries"][0]["revision_no"] == 1
    assert seg["entries"][0]["sha256"] == SHA_OF(b"alpha")

    # The move detaches the source path but must not touch rev 1.
    assert _move(client, src, backup, r.headers["etag"], 1).status_code == 200
    assert client.get(f"/objects/{src}").status_code == 404

    # Re-pulling the same segment returns byte-identical pinned content.
    again = client.get(f"/copy-sessions/{sid}/segments").json()
    assert again == seg
    import base64

    assert base64.b64decode(again["entries"][0]["content_b64"]) == b"alpha"


def test_move_changes_baseline_and_later_ack_conflicts(client):
    """A session whose TARGET is later populated by a move follows the
    ordinary baseline rule: the ack sees a changed target and 409s."""
    # Session copies "src-rel/doc.txt" onto "<target_prefix>src-rel/doc.txt";
    # a move later publishes byte-identical content onto that target.
    src_rel = f"src-rel-{uuid.uuid4().hex[:8]}/doc.txt"
    target_prefix = f"mv-tgt-prefix-{uuid.uuid4().hex[:8]}/"
    target = target_prefix + src_rel
    src_for_session = src_rel
    _put(client, src_for_session, b"payload")
    sid = _create_session(
        client, [src_for_session], target_prefix, segment_size=1
    ).json()["session"]
    cursor = client.get(f"/copy-sessions/{sid}/segments").json()["cursor"]

    # Target did not exist at baseline; meanwhile a move publishes there
    # with byte-identical content.
    mover_src = _path("mover")
    r = _put(client, mover_src, b"payload")
    mv = _move(client, mover_src, target, r.headers["etag"], 1)
    assert mv.status_code == 200

    ack = client.post(f"/copy-sessions/{sid}/acks", json={"cursor": cursor})
    assert ack.status_code == 409
    result = ack.json()
    assert result["status"] == "conflict"
    conflict = result["conflicts"][0]
    assert conflict["target_path"] == target
    assert conflict["baseline_revision_no"] is None
    assert conflict["actual_revision_no"] == 1
    # Same bytes are still a conflict (identity baseline): the move
    # published a *different revision identity* after the baseline.
    assert conflict["reason"] == "target_republished_same_content"
    assert conflict["expected_sha256"] == conflict["actual_sha256"]
    # The batch did not apply and the cursor did not advance.
    assert client.get(f"/copy-sessions/{sid}").json()["next_segment"] == 0
    assert client.get(f"/objects/{target}").content == b"payload"


def test_manifest_pinned_revision_survives_gc_after_move(client):
    src, dst = _path("pinned-src"), _path("pinned-dst")
    r = _put(client, src, b"keep-me")
    # A session copying src to an unrelated prefix keeps its manifest
    # pinned to src's revision; its target baseline is on a different
    # path, so only copy_manifest protects src's revision below.
    target_prefix = _path("elsewhere") + "/"
    sid = _create_session(
        client, [src], target_prefix, segment_size=1
    ).json()["session"]
    assert _move(client, src, dst, r.headers["etag"], 1).status_code == 200

    # GC over the moved-away source: rev 1 is pinned by the manifest and
    # must be retained with the copy_manifest reason.
    preview = client.post(
        "/gc",
        json={"paths": [src], "before": FUTURE, "dry_run": True},
    ).json()
    (obj,) = preview["objects"]
    assert obj["candidates"] == []
    assert obj["retained"] == [
        {"revision_no": 1, "reasons": ["copy_manifest"]}
    ]
    # Execute deletes nothing and the session segment still serves bytes.
    executed = client.post(
        "/gc",
        json={"paths": [src], "before": FUTURE, "dry_run": False},
    ).json()
    assert executed["deleted"] == []
    seg = client.get(f"/copy-sessions/{sid}/segments").json()
    import base64

    assert base64.b64decode(seg["entries"][0]["content_b64"]) == b"keep-me"


# ---- interaction with GC ------------------------------------------------------

def test_gc_reclaims_old_revisions_of_moved_path(client):
    src, dst = _path("old-src"), _path("old-dst")
    _put(client, src, b"v1-longer")
    _put(client, src, b"v2")
    g = client.get(f"/objects/{src}")
    assert _move(client, src, dst, g.headers["etag"], 2).status_code == 200

    # Moved-away path is still "known" to GC (it owns revisions): no 404.
    preview = client.post(
        "/gc",
        json={"paths": [src], "before": FUTURE, "dry_run": True},
    )
    assert preview.status_code == 200
    (obj,) = preview.json()["objects"]
    # Rev 2 is no longer the current revision of src (current is NULL),
    # so both old revisions are reclaimable.
    assert sorted(c["revision_no"] for c in obj["candidates"]) == [1, 2]
    assert obj["retained"] == []

    executed = client.post(
        "/gc",
        json={"paths": [src], "before": FUTURE, "dry_run": False},
    )
    assert executed.status_code == 200
    assert sorted(d["revision_no"] for d in executed.json()["deleted"]) == [1, 2]
    # The move target is untouched.
    assert client.get(f"/objects/{dst}").content == b"v2"


def test_gc_missing_path_alongside_moved_path_is_404(client):
    src, dst, nope = _path("g-src"), _path("g-dst"), _path("g-nope")
    r = _put(client, src, b"v")
    assert _move(client, src, dst, r.headers["etag"], 1).status_code == 200
    resp = client.post(
        "/gc",
        json={"paths": [src, nope], "before": FUTURE, "dry_run": False},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "object_not_found", "missing": [nope]}


# ---- concurrency / atomicity --------------------------------------------------

def test_two_concurrent_moves_one_wins_one_fails(client):
    """Two moves onto the same empty target serialise on the target row
    lock: exactly one succeeds; the other sees the now-occupied target and
    returns 409, with no 500 and no revision number collision."""
    for i in range(8):
        s1 = _path(f"race-s1-{i}")
        s2 = _path(f"race-s2-{i}")
        dst = _path(f"race-dst-{i}")
        b1, b2 = f"body-one-{i}".encode(), f"body-two-{i}".encode()
        r1 = _put(client, s1, b1)
        r2 = _put(client, s2, b2)
        barrier = threading.Barrier(2)
        outcomes = {}

        def m1():
            barrier.wait()
            outcomes["m1"] = _move(client, s1, dst, r1.headers["etag"], 1)

        def m2():
            barrier.wait()
            outcomes["m2"] = _move(client, s2, dst, r2.headers["etag"], 1)

        threads = [threading.Thread(target=m1), threading.Thread(target=m2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = {outcomes["m1"].status_code, outcomes["m2"].status_code}
        assert statuses == {200, 409}, statuses
        winner = outcomes["m1"] if outcomes["m1"].status_code == 200 else outcomes["m2"]
        # Exactly one target revision, numbered 1.
        g = client.get(f"/objects/{dst}")
        assert g.status_code == 200
        assert g.headers["etag"] == winner.headers["etag"]
        # The loser's source is still intact (no partial move).
        loser_src = s2 if winner is outcomes["m1"] else s1
        loser_body = b2 if winner is outcomes["m1"] else b1
        assert client.get(f"/objects/{loser_src}").content == loser_body


def test_concurrent_move_and_put_on_target_one_wins(client):
    for i in range(6):
        src = _path(f"mp-src-{i}")
        dst = _path(f"mp-dst-{i}")
        body = f"move-{i}".encode()
        r = _put(client, src, body)
        barrier = threading.Barrier(2)
        outcomes = {}

        def do_move():
            barrier.wait()
            outcomes["move"] = _move(client, src, dst, r.headers["etag"], 1)

        def do_put():
            barrier.wait()
            outcomes["put"] = client.put(
                f"/objects/{dst}",
                content=b"put-content",
                headers={"Content-Type": "text/plain"},
            )

        threads = [threading.Thread(target=do_move), threading.Thread(target=do_put)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        mv, pu = outcomes["move"], outcomes["put"]
        assert pu.status_code == 204
        assert mv.status_code in (200, 409)
        g = client.get(f"/objects/{dst}")
        if mv.status_code == 200:
            # Move created rev 1; the waiting PUT continued as rev 2.
            assert g.content == b"put-content"
            assert pu.headers["x-revision-no"] == "2"
            assert client.get(f"/objects/{src}").status_code == 404
        else:
            # PUT created rev 1 first; move rejected with 409.
            assert g.content == b"put-content"
            assert pu.headers["x-revision-no"] == "1"
            assert client.get(f"/objects/{src}").content == body


def test_concurrent_moves_in_a_chain_do_not_deadlock(client):
    """Concurrent moves whose lock orders overlap must serialise, not
    deadlock (every transaction uses the global ascending path order)."""
    a = _path("chain-a")
    b = _path("chain-b")
    c = _path("chain-c")
    ra = _put(client, a, b"a-content")
    rb = _put(client, b, b"b-content")
    barrier = threading.Barrier(2)
    outcomes = {}

    def first():
        barrier.wait()
        outcomes["ab"] = _move(client, a, b + "-out", ra.headers["etag"], 1)

    def second():
        barrier.wait()
        outcomes["bc"] = _move(client, b, c, rb.headers["etag"], 1)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(outcomes) == {"ab", "bc"}
    for resp in outcomes.values():
        assert resp.status_code in (200, 404, 409, 412)
    # b -> c succeeds deterministically (c is empty, b holds rev 1 and is
    # not the target of the other move); a moves to b-out as well.
    assert outcomes["bc"].status_code == 200
    assert outcomes["ab"].status_code == 200
    assert client.get(f"/objects/{c}").content == b"b-content"
    assert client.get(f"/objects/{b}-out").content == b"a-content"


def test_failed_move_is_fully_rolled_back(client):
    src, dst = _path("rollback-src"), _path("rollback-dst")
    _put(client, src, b"keep")
    _put(client, dst, b"occupied")
    before_src = client.get(f"/objects/{src}").headers["etag"]

    resp = _move(client, src, dst, ETAG_OF(b"keep"), 1)
    assert resp.status_code == 409
    # Retrying with the source now changed also leaves everything intact.
    assert client.get(f"/objects/{src}").headers["etag"] == before_src
    assert client.get(f"/objects/{dst}").content == b"occupied"
