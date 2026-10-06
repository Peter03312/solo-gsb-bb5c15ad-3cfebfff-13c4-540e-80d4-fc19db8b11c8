"""日志跨演练迁移（复核员发现日志归错演练）HTTP 集成测试。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("QSO_DB_PATH", tmp.name)
    from app.main import app

    with TestClient(app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _log(no, station, counterpart, t, band="40m", mode="SSB"):
    return {
        "record_no": no,
        "station": station,
        "counterpart": counterpart,
        "time": t,
        "band": band,
        "mode": mode,
    }


def _pair_keys(pairs):
    return sorted((p["record_a"], p["record_b"]) for p in pairs)


def _lock_keys(locks):
    return sorted((l["record_a"], l["record_b"]) for l in locks)


def _migrate(client, source, target, nos):
    return client.post(
        f"/drills/{source}/logs/migrate",
        json={"target_drill_id": target, "record_nos": nos},
    )


# --------------------------------------------------------------------------
# 成功路径
# --------------------------------------------------------------------------
def test_migrate_unpaired_record_pairs_under_target_allowance(client):
    # 源演练允许 10s；A1/B1 5s 配对，C1 无对方未配对。
    client.post("/drills", json={"drill_id": "S", "allowed_time_diff_seconds": 10})
    # 目标演练只允许 2s；已有 D1 在 C1 之后 1s。
    client.post("/drills", json={"drill_id": "T", "allowed_time_diff_seconds": 2})
    client.post("/drills/S/logs", json={"logs": [
        _log("A1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("B1", "B", "A", "2026-10-04T00:00:05+00:00"),
        _log("C1", "C", "D", "2026-10-04T00:00:00+00:00"),
    ]})
    client.put(
        "/drills/T/logs/D1",
        json=_log("D1", "D", "C", "2026-10-04T00:00:01+00:00"),
    )

    r = _migrate(client, "S", "T", ["C1"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_drill_id"] == "S"
    assert body["target_drill_id"] == "T"
    assert body["migrated_records"] == ["C1"]

    # 两端各只推进一次版本。
    assert body["source_result"]["drill_id"] == "S"
    assert body["source_result"]["version"] == 2
    assert body["target_result"]["drill_id"] == "T"
    assert body["target_result"]["version"] == 2

    # 源：C1 已离开，A1/B1 仍配对；目标：按目标允许时差 C1/D1（1s）配对。
    assert _pair_keys(body["source_result"]["pairs"]) == [("A1", "B1")]
    assert body["source_result"]["unpaired"] == []
    assert _pair_keys(body["target_result"]["pairs"]) == [("C1", "D1")]
    assert body["target_result"]["pairs"][0]["time_diff_seconds"] == 1
    assert body["target_result"]["unpaired"] == []

    # 日志确实从源移到目标（GET 日志接口保持兼容）。
    src_nos = [l["record_no"] for l in client.get("/drills/S/logs").json()]
    tgt_nos = [l["record_no"] for l in client.get("/drills/T/logs").json()]
    assert src_nos == ["A1", "B1"]
    assert set(tgt_nos) == {"C1", "D1"}

    # 两端结果查询与迁移响应一致。
    assert client.get("/drills/S/result").json()["version"] == 2
    assert client.get("/drills/T/result").json()["version"] == 2


def test_migrate_preserves_original_log_fields(client):
    client.post("/drills", json={"drill_id": "S", "allowed_time_diff_seconds": 10})
    client.post("/drills", json={"drill_id": "T", "allowed_time_diff_seconds": 10})
    client.put(
        "/drills/S/logs/R9",
        json=_log("R9", "bh1aaa", "bh2bbb",
                  "2026-10-04T08:00:00+08:00", band="40m", mode="ssb"),
    )
    r = _migrate(client, "S", "T", ["R9"])
    assert r.status_code == 200, r.text
    moved = [l for l in client.get("/drills/T/logs").json()
             if l["record_no"] == "R9"][0]
    # 呼号/波段/模式规范化结果、带时区时间换算结果原样保留。
    assert moved["station"] == "BH1AAA"
    assert moved["counterpart"] == "BH2BBB"
    assert moved["band"] == "40M"
    assert moved["mode"] == "SSB"
    assert moved["time"] == "2026-10-04T00:00:00+00:00"
    assert "updated_at" in moved
    # 源演练已无该记录。
    assert client.get("/drills/S/logs").json() == []


def test_migrate_multiple_records_and_target_allowance_boundary(client):
    # 源允许 100s：E1/F1（10s）配对；目标允许 3s：迁入后超时差，不配对。
    client.post("/drills", json={"drill_id": "S2", "allowed_time_diff_seconds": 100})
    client.post("/drills", json={"drill_id": "T2", "allowed_time_diff_seconds": 3})
    client.post("/drills/S2/logs", json={"logs": [
        _log("E1", "E", "F", "2026-10-04T00:00:00+00:00"),
        _log("F1", "F", "E", "2026-10-04T00:00:10+00:00"),
    ]})
    r = _migrate(client, "S2", "T2", ["F1", "E1"])  # 乱序也可
    assert r.status_code == 200, r.text
    body = r.json()
    # 返回的迁移清单按记录号升序确定化。
    assert body["migrated_records"] == ["E1", "F1"]
    # 源空场、目标两条均未配对（按目标允许时差重新互证）。
    assert body["source_result"]["pairs"] == []
    assert body["source_result"]["unpaired"] == []
    assert body["target_result"]["pairs"] == []
    assert sorted(body["target_result"]["unpaired"]) == ["E1", "F1"]
    assert body["source_result"]["version"] == 2
    assert body["target_result"]["version"] == 1


def test_migrate_each_side_bumps_once_and_persists_snapshot(client):
    client.post("/drills", json={"drill_id": "S", "allowed_time_diff_seconds": 10})
    client.post("/drills", json={"drill_id": "T", "allowed_time_diff_seconds": 10})
    client.post("/drills/S/logs", json={"logs": [
        _log("C1", "C", "D", "2026-10-04T00:00:00+00:00"),
    ]})
    client.put(
        "/drills/T/logs/D1",
        json=_log("D1", "D", "C", "2026-10-04T00:00:00+00:00"),
    )
    src_before = client.get("/drills/S/result").json()["version"]
    tgt_before = client.get("/drills/T/result").json()["version"]
    r = _migrate(client, "S", "T", ["C1"])
    assert r.status_code == 200, r.text
    assert r.json()["source_result"]["version"] == src_before + 1
    assert r.json()["target_result"]["version"] == tgt_before + 1

    # 两端都留存了新版快照。
    assert client.get("/drills/S/snapshots").json()["versions"] == [0, 1, 2]
    assert client.get("/drills/T/snapshots").json()["versions"] == [0, 1, 2]
    new_src = client.get("/drills/S/snapshots/2").json()
    new_tgt = client.get("/drills/T/snapshots/2").json()
    assert new_src["unpaired"] == [] and new_src["pairs"] == []
    assert _pair_keys(new_tgt["pairs"]) == [("C1", "D1")]

    # 历史快照不可改写：源 v1 中 C1 仍是未配对。
    old_src = client.get("/drills/S/snapshots/1").json()
    assert old_src["unpaired"] == ["C1"]


def test_unmigrated_source_lock_survives_and_target_lock_survives(client):
    client.post("/drills", json={"drill_id": "S", "allowed_time_diff_seconds": 100})
    client.post("/drills", json={"drill_id": "T", "allowed_time_diff_seconds": 100})
    client.post("/drills/S/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("X1", "X", "Y", "2026-10-04T00:00:00+00:00"),
    ]})
    client.put("/drills/S/locks", json={"record_a": "L1", "record_b": "R1"})
    client.post("/drills/T/logs", json={"logs": [
        _log("P1", "P", "Q", "2026-10-04T00:00:00+00:00"),
        _log("Q1", "Q", "P", "2026-10-04T00:00:00+00:00"),
        _log("Y1", "Y", "X", "2026-10-04T00:00:00+00:00"),
    ]})
    client.put("/drills/T/locks", json={"record_a": "P1", "record_b": "Q1"})

    r = _migrate(client, "S", "T", ["X1"])
    assert r.status_code == 200, r.text
    # 未迁移的源锁定照常强制入选；目标既有锁定保留，迁入记录自由配对。
    assert _pair_keys(r.json()["source_result"]["pairs"]) == [("L1", "R1")]
    assert _pair_keys(r.json()["target_result"]["pairs"]) == [
        ("P1", "Q1"), ("X1", "Y1"),
    ]
    assert _lock_keys(client.get("/drills/S/locks").json()["locks"]) == [
        ("L1", "R1")
    ]
    assert _lock_keys(client.get("/drills/T/locks").json()["locks"]) == [
        ("P1", "Q1")
    ]
    # 新快照同样带上留存时的锁定清单。
    sv = client.get("/drills/S/snapshots/3").json()
    tv = client.get("/drills/T/snapshots/2").json()
    assert _lock_keys(sv["locks"]) == [("L1", "R1")]
    assert _lock_keys(tv["locks"]) == [("P1", "Q1")]


# --------------------------------------------------------------------------
# 拒绝路径：整次拒绝、两端状态不变
# --------------------------------------------------------------------------
def _seed_locked_source(client, src="S", tgt="T"):
    client.post("/drills", json={"drill_id": src, "allowed_time_diff_seconds": 100})
    client.post("/drills", json={"drill_id": tgt, "allowed_time_diff_seconds": 100})
    client.post(f"/drills/{src}/logs", json={"logs": [
        _log("L1", "A", "B", "2026-10-04T00:00:00+00:00"),
        _log("R1", "B", "A", "2026-10-04T00:00:00+00:00"),
        _log("X1", "C", "D", "2026-10-04T00:00:00+00:00"),
    ]})
    client.put(f"/drills/{src}/locks", json={"record_a": "L1", "record_b": "R1"})
    client.put(
        f"/drills/{tgt}/logs/R1",
        json=_log("R1", "Z", "Y", "2026-10-04T00:00:00+00:00"),
    )


def test_migrate_rejects_locked_record(client):
    _seed_locked_source(client)
    # 记录是源锁定对的任一端都拒绝。
    r = _migrate(client, "S", "T", ["L1"])
    assert r.status_code == 409
    assert r.json()["error"] == "migration_record_locked"
    # 锁定记录与其他正常记录混在一起也整次拒绝（L1 在目标无同号记录）。
    r = _migrate(client, "S", "T", ["L1", "X1"])
    assert r.status_code == 409
    assert r.json()["error"] == "migration_record_locked"


def test_migrate_rejects_missing_source_record(client):
    _seed_locked_source(client)
    r = _migrate(client, "S", "T", ["NOPE"])
    assert r.status_code == 404
    assert r.json()["error"] == "migration_record_missing"
    # 部分存在部分缺失也整次拒绝。
    r = _migrate(client, "S", "T", ["X1", "NOPE"])
    assert r.status_code == 404
    assert r.json()["error"] == "migration_record_missing"


def test_migrate_rejects_target_same_record_no(client):
    _seed_locked_source(client)
    r = _migrate(client, "S", "T", ["X1", "R1"])
    assert r.status_code == 409
    assert r.json()["error"] == "migration_target_record_exists"


def test_migrate_rejects_duplicate_record_nos(client):
    _seed_locked_source(client)
    r = _migrate(client, "S", "T", ["X1", "X1"])
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_record_in_batch"


def test_migrate_rejects_empty_or_blank_record_nos(client):
    _seed_locked_source(client)
    r = _migrate(client, "S", "T", [])
    assert r.status_code == 422
    r = client.post(
        "/drills/S/logs/migrate",
        json={"target_drill_id": "T", "record_nos": ["   "]},
    )
    assert r.status_code == 422
    r = client.post(
        "/drills/S/logs/migrate",
        json={"target_drill_id": "  ", "record_nos": ["X1"]},
    )
    assert r.status_code == 422


def test_migrate_rejects_same_drill(client):
    _seed_locked_source(client)
    r = _migrate(client, "S", "S", ["X1"])
    assert r.status_code == 422
    assert r.json()["error"] == "migration_same_drill"


def test_migrate_rejects_unknown_and_sealed_drills(client):
    _seed_locked_source(client)
    client.post("/drills", json={"drill_id": "DONE", "allowed_time_diff_seconds": 1})
    client.post("/drills/DONE/seal")

    assert _migrate(client, "GHOST", "T", ["X1"]).status_code == 404
    assert _migrate(client, "S", "GHOST", ["X1"]).status_code == 404
    r = _migrate(client, "DONE", "T", ["X1"])
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"
    r = _migrate(client, "S", "DONE", ["X1"])
    assert r.status_code == 409 and r.json()["error"] == "drill_sealed"


def test_rejected_migration_leaves_both_sides_unchanged(client):
    _seed_locked_source(client)
    src_before = client.get("/drills/S/result").json()
    tgt_before = client.get("/drills/T/result").json()

    for payload in (
        {"target_drill_id": "T", "record_nos": ["L1"]},       # 被锁定
        {"target_drill_id": "T", "record_nos": ["NOPE"]},     # 源不存在
        {"target_drill_id": "T", "record_nos": ["R1"]},       # 目标同号
        {"target_drill_id": "T", "record_nos": ["X1", "X1"]}, # 列表重复
        {"target_drill_id": "S", "record_nos": ["X1"]},       # 同演练
    ):
        r = client.post("/drills/S/logs/migrate", json=payload)
        assert r.status_code in (404, 409, 422), payload

    assert client.get("/drills/S/result").json() == src_before
    assert client.get("/drills/T/result").json() == tgt_before
    # 版本、日志、锁定均不变；没有产生多余快照。
    assert client.get("/drills/S/snapshots").json()["versions"] == [0, 1, 2]
    assert client.get("/drills/T/snapshots").json()["versions"] == [0, 1]
    assert [l["record_no"] for l in client.get("/drills/S/logs").json()] == [
        "L1", "R1", "X1",
    ]
    assert [l["record_no"] for l in client.get("/drills/T/logs").json()] == ["R1"]
    assert _lock_keys(client.get("/drills/S/locks").json()["locks"]) == [
        ("L1", "R1")
    ]
