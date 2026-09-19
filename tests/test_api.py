"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts, reference, tolerance, readings):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance, "readings": readings},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def revise_observation(client, obs_id, ts, reference, tolerance, readings, revision):
    resp = client.put(
        f"/observations/{obs_id}",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance,
              "readings": readings, "revision": revision},
    )
    return resp


# ---------- 观测登记 ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_observation_marks_outliers_and_computes_bias(client):
    body = make_observation(client, 1000.0, reference=10.0, tolerance=0.5,
                            readings=[9.0, 9.1, 8.9, 20.0])
    # 组中位数 9.05，20.0 偏离超差判为异常；有效读数中位数 9.0 → 偏差 10.0 - 9.0 = 1.0
    assert body["group_median"] == pytest.approx(9.05)
    assert body["bias"] == pytest.approx(1.0)
    assert body["outliers"] == [20.0]
    flags = {r["value"]: r["is_outlier"] for r in body["readings"]}
    assert flags == {9.0: False, 9.1: False, 8.9: False, 20.0: True}


def test_raw_readings_are_preserved(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    obs = client.get("/observations").json()
    assert len(obs) == 1
    assert sorted(r["value"] for r in obs[0]["readings"]) == [8.9, 9.0, 9.1, 20.0]


def test_fewer_than_three_readings_rejected(client):
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.1,
                             "readings": [1.0, 1.1]})
    assert resp.status_code == 422


def test_all_outliers_rejected(client):
    # 容差为 0 时四个读数两两不同，全部偏离中位数 → 无法计算偏差
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.0,
                             "readings": [1.0, 2.0, 3.0, 4.0]})
    assert resp.status_code == 422
    assert "cannot compute a calibration bias" in resp.json()["detail"]


def test_duplicate_timestamp_rejected(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
                             "readings": [9.0, 9.1, 8.9]})
    assert resp.status_code == 409


def test_iso8601_timestamp_accepted(client):
    body = make_observation(client, "1970-01-01T00:16:40Z", 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["timestamp"] == pytest.approx(1000.0)


# ---------- 漂移补偿 ----------

@pytest.fixture()
def two_points(client):
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5, readings=[9.0, 9.1, 8.9, 20.0])  # bias 1.0
    make_observation(client, 2000.0, reference=10.0, tolerance=0.5, readings=[7.0, 7.1, 6.9])        # bias 3.0
    return client


def test_correction_interpolates_linearly(two_points):
    resp = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bias"] == pytest.approx(2.0)        # 1.0 与 3.0 的中点
    assert body["corrected"] == pytest.approx(7.0)   # 读数 + 偏差
    assert body["interval"]["t_start"] == 1000.0
    assert body["interval"]["t_end"] == 2000.0
    assert body["interval"]["bias_start"] == pytest.approx(1.0)
    assert body["interval"]["bias_end"] == pytest.approx(3.0)


def test_correction_reports_endpoint_anomalies(two_points):
    body = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0}).json()
    assert len(body["endpoints"]) == 2
    assert body["endpoints"][0]["outliers"] == [20.0]
    assert body["endpoints"][1]["outliers"] == []


def test_correction_at_exact_observation_point(two_points):
    body = two_points.post("/corrections", json={"timestamp": 2000.0, "reading": 5.0}).json()
    assert body["bias"] == pytest.approx(3.0)
    assert body["corrected"] == pytest.approx(8.0)
    assert body["interval"] is None
    assert len(body["endpoints"]) == 1


def test_correction_outside_coverage_rejected(two_points):
    for ts in (999.0, 2001.0):
        resp = two_points.post("/corrections", json={"timestamp": ts, "reading": 5.0})
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_correction_without_any_observation_rejected(client):
    resp = client.post("/corrections", json={"timestamp": 1.0, "reading": 5.0})
    assert resp.status_code == 400


# ---------- 区间重算 ----------

def test_adjacent_intervals_recomputed_after_insert(two_points):
    # 在 1000 与 2000 之间插入新观测（bias 2.0）
    make_observation(two_points, 1500.0, reference=10.0, tolerance=0.5, readings=[8.0, 8.1, 7.9])
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1000.0, 1500.0), (1500.0, 2000.0)]

    # 旧区间中点 1250 现在应基于新区间 (1000,1500) 插值：bias = 1.5
    body = two_points.post("/corrections", json={"timestamp": 1250.0, "reading": 10.0}).json()
    assert body["bias"] == pytest.approx(1.5)
    assert body["corrected"] == pytest.approx(11.5)


def test_intervals_extend_at_edges(two_points):
    make_observation(two_points, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4])   # bias 0.5
    make_observation(two_points, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])  # bias 4.0
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(500.0, 1000.0), (1000.0, 2000.0), (2000.0, 3000.0)]
    # 覆盖范围随之扩展
    assert two_points.post("/corrections", json={"timestamp": 750.0, "reading": 0.0}).status_code == 200
    assert two_points.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).status_code == 200


# ---------- 观测修订 ----------

def test_new_observation_starts_at_revision_1(client):
    body = make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["revision"] == 1


def test_revise_same_position_recomputes_bias_and_intervals(two_points):
    # 将 1000 处观测的偏差从 1.0 修订为 0.0（读数全部贴近参考值）
    resp = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == 1                 # ID 保持不变
    assert body["revision"] == 2           # 版本推进
    assert body["bias"] == pytest.approx(0.0)
    assert body["outliers"] == []

    # 区间端点偏差随之更新
    interval = two_points.get("/intervals").json()
    assert [(iv["t_start"], iv["t_end"]) for iv in interval] == [(1000.0, 2000.0)]
    assert interval[0]["bias_start"] == pytest.approx(0.0)
    assert interval[0]["bias_end"] == pytest.approx(3.0)
    # 中点插值偏差由 2.0 变为 1.5
    body = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(1.5)


def test_revise_moves_observation_and_rebuilds_both_locations(two_points):
    # 把 1000 处观测（id=1）前移到 500：原位置缺口接通，新位置一分为二
    resp = revise_observation(two_points, 1, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(500.0, 2000.0)]      # 1000 不再是观测点，区间不被拆分


def test_revise_into_gap_splits_new_interval(two_points):
    # 把 1000 处观测移动到 1000 与 2000 之间的 1500
    resp = revise_observation(two_points, 1, 1500.0, 10.0, 0.5, [8.0, 8.1, 7.9], revision=1)
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1500.0, 2000.0)]     # 原位置 1000 左侧已无观测，缺口无需接通


def test_revise_middle_point_in_three_point_chain(client):
    # 三点链 1000(bias1) / 2000(bias3) / 3000(bias4)，把中间点移到 1500(bias2)
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    make_observation(client, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9])
    make_observation(client, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])
    resp = revise_observation(client, 2, 1500.0, 10.0, 0.5, [8.0, 8.1, 7.9], revision=1)
    assert resp.status_code == 200, resp.text
    intervals = client.get("/intervals").json()
    # 原位置缺口 (1000,3000) 不得被接通——观测自身已落在新位置 1500
    assert [(iv["t_start"], iv["t_end"]) for iv in intervals] == [
        (1000.0, 1500.0), (1500.0, 3000.0),
    ]
    assert client.post("/corrections", json={"timestamp": 1250.0, "reading": 0.0}).json()["bias"] \
        == pytest.approx(1.5)
    assert client.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).json()["bias"] \
        == pytest.approx(10 / 3)  # 新区间 (1500 bias2, 3000 bias4) 的 2/3 处
    # 再移回 2000（小幅右移越过旧缺口左侧），区间仍应恰好两段
    resp = revise_observation(client, 2, 2000.0, 10.0, 0.5, [7.0, 7.1, 6.9], revision=2)
    assert resp.status_code == 200, resp.text
    assert [(iv["t_start"], iv["t_end"]) for iv in client.get("/intervals").json()] == [
        (1000.0, 2000.0), (2000.0, 3000.0),
    ]


def test_revise_beyond_edge_extends_coverage(two_points):
    # 把 1000 处观测移动到 3000，覆盖范围变为 [2000, 3000]
    resp = revise_observation(two_points, 1, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9], revision=1)
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(2000.0, 3000.0)]
    assert two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 0.0}).status_code == 400
    assert two_points.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).status_code == 200


def test_revise_stale_revision_conflicts(two_points):
    ok = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert ok.status_code == 200
    stale = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert stale.status_code == 409
    # 冲突时数据未被二次修订
    obs = next(o for o in two_points.get("/observations").json() if o["id"] == 1)
    assert obs["revision"] == 2


def test_revise_timestamp_clash_conflicts(two_points):
    # 把 1000 处观测改到已被 2000 处观测占用的时点 → 409，区间维持原样
    resp = revise_observation(two_points, 1, 2000.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 409
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1000.0, 2000.0)]


def test_revise_unknown_id_returns_404(two_points):
    resp = revise_observation(two_points, 999, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 404


def test_revise_invalid_readings_returns_422(two_points):
    resp = revise_observation(two_points, 1, 1000.0, 10.0, 0.0, [1.0, 2.0, 3.0, 4.0], revision=1)
    assert resp.status_code == 422


def test_revisions_history_returns_snapshots_newest_first(two_points):
    # 第一次修订（revision 1 -> 2）：1000 → 900，偏差 1.0 → 约 0.5
    r1 = revise_observation(two_points, 1, 900.0, 10.0, 0.5, [9.5, 9.6, 9.4], revision=1)
    assert r1.status_code == 200
    # 第二次修订（revision 2 -> 3）：900 → 800
    r2 = revise_observation(two_points, 1, 800.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=2)
    assert r2.status_code == 200

    resp = two_points.get("/observations/1/revisions")
    assert resp.status_code == 200
    history = resp.json()
    assert [h["revision"] for h in history] == [2, 1]   # 按版本倒序

    snap_v1 = history[1]
    assert snap_v1["old_timestamp"] == 1000.0
    assert snap_v1["old_timestamp_iso"].startswith("1970-01-01T00:16:40")
    assert snap_v1["old_reference"] == 10.0
    assert snap_v1["old_tolerance"] == 0.5
    assert snap_v1["old_bias"] == pytest.approx(1.0)
    assert snap_v1["old_readings"] == [9.0, 9.1, 8.9, 20.0]
    assert "changed_at" in snap_v1 and "changed_at_iso" in snap_v1

    snap_v2 = history[0]
    assert snap_v2["old_timestamp"] == 900.0
    assert snap_v2["old_readings"] == [9.5, 9.6, 9.4]


def test_revisions_history_empty_before_any_revision(two_points):
    assert two_points.get("/observations/1/revisions").json() == []


def test_revisions_history_unknown_id_returns_404(two_points):
    assert two_points.get("/observations/999/revisions").status_code == 404


# ---------- 累计漂移积分（解析积分，禁止采样） ----------

@pytest.fixture()
def crossing_chain(client):
    """三点两区间：1000(bias +2) → 2000(bias -2) → 3000(bias +4)，两段均过零。"""
    make_observation(client, 1000.0, reference=10.0, tolerance=5.0, readings=[8.0, 8.1, 7.9])
    make_observation(client, 2000.0, reference=10.0, tolerance=5.0, readings=[12.0, 12.1, 11.9])
    make_observation(client, 3000.0, reference=10.0, tolerance=5.0, readings=[6.0, 6.1, 5.9])
    return client


def drift_integral(client, start, end):
    resp = client.get("/drift-integral", params={"from": start, "to": end})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_drift_integral_full_span_exact_trapezoids_and_crossing_split(crossing_chain):
    body = drift_integral(crossing_chain, 1000.0, 3000.0)
    first, second = body["contributions"]

    # 段一 +2 → -2：有号面积 0；过零 1500 拆出两个三角形，绝对面积 1000
    assert first["t_start"] == 1000.0 and first["t_end"] == 2000.0
    assert first["signed_bias_seconds"] == pytest.approx(0.0)
    assert first["absolute_bias_seconds"] == pytest.approx(1000.0)
    assert first["zero_crossing"] == pytest.approx(1500.0)

    # 段二 -2 → +4：有号面积 1000；解析交点 2333.333…，绝对面积 5000/3
    assert second["t_start"] == 2000.0 and second["t_end"] == 3000.0
    assert second["signed_bias_seconds"] == pytest.approx(1000.0)
    assert second["zero_crossing"] == pytest.approx(7000.0 / 3.0)
    assert second["absolute_bias_seconds"] == pytest.approx(5000.0 / 3.0)

    # 汇总
    assert body["signed_bias_seconds"] == pytest.approx(1000.0)
    assert body["absolute_bias_seconds"] == pytest.approx(1000.0 + 5000.0 / 3.0)


def test_drift_integral_clips_to_query_bounds_inside_intervals(crossing_chain):
    # 窗口 [1250, 2250] 落在两个区间内部：
    # 段一裁剪 [1250,2000]：bias +1 → -2（1500 过零）
    # 段二裁剪 [2000,2250]：bias -2 → -0.5（同号，不拆分）
    body = drift_integral(crossing_chain, 1250.0, 2250.0)
    first, second = body["contributions"]

    assert (first["t_start"], first["t_end"]) == (1250.0, 2000.0)
    assert first["bias_start"] == pytest.approx(1.0)
    assert first["bias_end"] == pytest.approx(-2.0)
    assert first["zero_crossing"] == pytest.approx(1500.0)
    # 两个三角形：1·250/2 + 2·500/2 = 625
    assert first["absolute_bias_seconds"] == pytest.approx(625.0)
    assert first["signed_bias_seconds"] == pytest.approx(-375.0)

    assert (second["t_start"], second["t_end"]) == (2000.0, 2250.0)
    assert second["bias_start"] == pytest.approx(-2.0)
    assert second["bias_end"] == pytest.approx(-0.5)
    assert second["zero_crossing"] is None
    assert second["signed_bias_seconds"] == pytest.approx(-312.5)
    assert second["absolute_bias_seconds"] == pytest.approx(312.5)

    assert body["signed_bias_seconds"] == pytest.approx(-687.5)
    assert body["absolute_bias_seconds"] == pytest.approx(937.5)


def test_drift_integral_contributions_sorted_and_sums_equal_totals(crossing_chain):
    body = drift_integral(crossing_chain, 1000.0, 3000.0)
    starts = [c["t_start"] for c in body["contributions"]]
    assert starts == sorted(starts)
    assert sum(c["signed_bias_seconds"] for c in body["contributions"]) == \
        pytest.approx(body["signed_bias_seconds"])
    assert sum(c["absolute_bias_seconds"] for c in body["contributions"]) == \
        pytest.approx(body["absolute_bias_seconds"])


def test_drift_integral_bounds_on_coverage_edges_are_allowed(crossing_chain):
    body = drift_integral(crossing_chain, 1000.0, 2000.0)
    assert len(body["contributions"]) == 1
    assert body["absolute_bias_seconds"] == pytest.approx(1000.0)


def test_drift_integral_iso8601_query_params(crossing_chain):
    # 1500 → 2400
    body = drift_integral(
        crossing_chain, "1970-01-01T00:25:00Z", "1970-01-01T00:40:00Z"
    )
    assert body["from"] == pytest.approx(1500.0)
    assert body["to"] == pytest.approx(2400.0)
    assert body["signed_bias_seconds"] == pytest.approx(-820.0)
    assert body["absolute_bias_seconds"] == pytest.approx(846.6666666666667)


def test_drift_integral_from_must_be_less_than_to(crossing_chain):
    for start, end in ((2000.0, 2000.0), (2001.0, 2000.0)):
        resp = crossing_chain.get("/drift-integral", params={"from": start, "to": end})
        assert resp.status_code == 400
        assert "must be strictly less than" in resp.json()["detail"]


def test_drift_integral_outside_coverage_returns_400(crossing_chain):
    for start, end in ((999.0, 1500.0), (2500.0, 3001.0), (500.0, 3500.0)):
        resp = crossing_chain.get("/drift-integral", params={"from": start, "to": end})
        assert resp.status_code == 400
        assert "outside drift coverage" in resp.json()["detail"]


def test_drift_integral_without_any_observation_returns_400(client):
    resp = client.get("/drift-integral", params={"from": 1.0, "to": 2.0})
    assert resp.status_code == 400
    assert "no observations yet" in resp.json()["detail"]


def test_drift_integral_single_observation_rejects_any_positive_window(client):
    # 仅一个观测点时覆盖范围退化为单点 [1000, 1000]：任何 from < to 都越界
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5, readings=[9.0, 9.1, 8.9])
    resp = client.get("/drift-integral", params={"from": 999.999, "to": 1000.0})
    assert resp.status_code == 400
    assert "outside drift coverage" in resp.json()["detail"]


def test_drift_integral_uses_revised_biases(two_points):
    # 修订 1000 处偏差 1.0 → 0.0 后，[1000,2000] 有号面积应为 1500 而非 2000
    resp = revise_observation(two_points, 1, 1000.0, 10.0, 0.5, [10.0, 10.1, 9.9], revision=1)
    assert resp.status_code == 200
    body = drift_integral(two_points, 1000.0, 2000.0)
    assert body["signed_bias_seconds"] == pytest.approx(1500.0)
    assert body["contributions"][0]["bias_start"] == pytest.approx(0.0)
    assert body["contributions"][0]["bias_end"] == pytest.approx(3.0)


# ---------- 解析积分纯函数 ----------

def test_integrate_segment_same_sign_trapezoid():
    from app.calibration import integrate_segment
    signed, absolute, crossing = integrate_segment(0.0, 10.0, 1.0, 3.0)
    assert signed == pytest.approx(20.0)
    assert absolute == pytest.approx(20.0)
    assert crossing is None


def test_integrate_segment_zero_at_endpoint_not_split():
    from app.calibration import integrate_segment
    signed, absolute, crossing = integrate_segment(0.0, 10.0, 0.0, 4.0)
    assert signed == pytest.approx(20.0)
    assert absolute == pytest.approx(20.0)
    assert crossing is None  # 端点恰为 0，不属于段内过零


def test_integrate_segment_analytic_crossing_no_sampling():
    from app.calibration import integrate_segment
    signed, absolute, crossing = integrate_segment(0.0, 1000.0, 2.0, -2.0)
    assert signed == pytest.approx(0.0)
    assert crossing == pytest.approx(500.0)
    # 两个三角形：2·500/2 + 2·500/2
    assert absolute == pytest.approx(1000.0)
