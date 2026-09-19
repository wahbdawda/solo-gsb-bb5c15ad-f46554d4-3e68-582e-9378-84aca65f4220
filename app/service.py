"""业务服务层：观测入库、相邻区间重算、漂移补偿查询。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

from . import calibration
from .db import connect
from .models import epoch_to_iso


class DuplicateTimestampError(Exception):
    """同一时点已存在观测。"""


class ObservationNotFoundError(Exception):
    """指定 ID 的观测不存在。"""


class RevisionConflictError(Exception):
    """客户端携带的版本号与当前版本不一致（乐观锁冲突）。"""


class OutOfCoverageError(Exception):
    """查询时点超出已观测覆盖范围。"""

    def __init__(self, ts: float, lo: float | None, hi: float | None):
        self.ts, self.lo, self.hi = ts, lo, hi
        if lo is None:
            msg = "no observations yet; correction coverage is empty"
        else:
            msg = (
                f"timestamp {ts} is outside calibration coverage "
                f"[{lo}, {hi}] ({epoch_to_iso(lo)} .. {epoch_to_iso(hi)})"
            )
        super().__init__(msg)


# ---------- 观测 ----------

def create_observation(reference: float, tolerance: float, readings: list[float], ts: float) -> dict:
    bias, group_median, _inliers, _outliers = calibration.compute_bias(reference, tolerance, readings)
    with closing(connect()) as conn:
        with conn:  # 事务：观测、读数、区间重算要么全部成功要么全部回滚
            if conn.execute("SELECT 1 FROM observations WHERE ts = ?", (ts,)).fetchone():
                raise DuplicateTimestampError(f"an observation already exists at timestamp {ts}")
            cur = conn.execute(
                "INSERT INTO observations (ts, reference, tolerance, group_median, bias, revision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, reference, tolerance, group_median, bias, 1, time.time()),
            )
            obs_id = cur.lastrowid
            # 原始读数逐条保留，异常仅打标记，从不修改或删除
            conn.executemany(
                "INSERT INTO readings (observation_id, value, is_outlier) VALUES (?, ?, ?)",
                [
                    (obs_id, value, 1 if abs(value - group_median) > tolerance else 0)
                    for value in readings
                ],
            )
            _rebuild_adjacent_intervals(conn, ts)
            row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            return _observation_dict(conn, row)


def _rebuild_adjacent_intervals(conn: sqlite3.Connection, ts: float) -> None:
    """新增观测后仅重算与其相邻的区间。

    被新时点一分为二的旧区间删除，再补建 (prev, ts) 与 (ts, next) 两段；
    其余区间不受影响，保持原样。
    """
    conn.execute("DELETE FROM intervals WHERE t_start < ? AND t_end > ?", (ts, ts))
    cur = conn.execute("SELECT id, ts, bias FROM observations WHERE ts = ?", (ts,)).fetchone()
    _split_gap_with(conn, cur)


def _split_gap_with(conn: sqlite3.Connection, cur: sqlite3.Row) -> None:
    """以观测 cur 为新端点，补建 (prev, cur) 与 (cur, next) 两段插值区间。"""
    prev = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts < ? ORDER BY ts DESC LIMIT 1", (cur["ts"],)
    ).fetchone()
    nxt = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts > ? ORDER BY ts ASC LIMIT 1", (cur["ts"],)
    ).fetchone()
    if prev:
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (prev["ts"], cur["ts"], prev["bias"], cur["bias"], prev["id"], cur["id"]),
        )
    if nxt:
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (cur["ts"], nxt["ts"], cur["bias"], nxt["bias"], cur["id"], nxt["id"]),
        )


def revise_observation(
    obs_id: int,
    reference: float,
    tolerance: float,
    readings: list[float],
    ts: float,
    expected_revision: int,
) -> dict:
    """修订一次观测：重新判定异常、重算偏差，并重建原位置与新位置的相邻区间。

    单个事务内完成：乐观锁版本校验 → 目标时点冲突校验 → 保存修订前快照 →
    更新观测与读数 → 删除原/新位置受影响区间并补建。观测 ID 保持不变，
    成功后 revision +1。
    """
    # 入参本身可能无法算出偏差（读数不足/全部异常），在开事务前先校验
    bias, group_median, _inliers, _outliers = calibration.compute_bias(reference, tolerance, readings)
    with closing(connect()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            if row is None:
                raise ObservationNotFoundError(f"no observation with id {obs_id}")
            if row["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"observation {obs_id} is at revision {row['revision']}, "
                    f"not {expected_revision}; fetch the latest observation and retry"
                )
            clash = conn.execute(
                "SELECT id FROM observations WHERE ts = ? AND id <> ?", (ts, obs_id)
            ).fetchone()
            if clash is not None:
                raise DuplicateTimestampError(f"an observation already exists at timestamp {ts}")

            old_ts = row["ts"]
            old_reading_rows = conn.execute(
                "SELECT value FROM readings WHERE observation_id = ? ORDER BY id", (obs_id,)
            ).fetchall()
            try:
                # 修订前快照：旧输入与旧偏差，按旧版本号归档。
                # 并发修订者会在快照唯一约束 (observation_id, revision) 上被拦住
                conn.execute(
                    "INSERT INTO revisions (observation_id, revision, changed_at, old_ts, old_reference,"
                    " old_tolerance, old_bias, old_readings)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        obs_id, row["revision"], time.time(), old_ts, row["reference"],
                        row["tolerance"], row["bias"], json.dumps([r["value"] for r in old_reading_rows]),
                    ),
                )
                # 条件更新作为原子的比较与交换：revision 已被他人推进时命中 0 行
                cur = conn.execute(
                    "UPDATE observations SET ts = ?, reference = ?, tolerance = ?, group_median = ?,"
                    " bias = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
                    (ts, reference, tolerance, group_median, bias, obs_id, expected_revision),
                )
            except sqlite3.IntegrityError as exc:
                if "revisions." in str(exc):
                    raise RevisionConflictError(
                        f"observation {obs_id} was revised concurrently"
                        f" (expected revision {expected_revision}); fetch the latest and retry"
                    ) from exc
                # observations.ts 唯一约束：并发下目标时点被其它事务抢先占用
                raise DuplicateTimestampError(
                    f"an observation already exists at timestamp {ts}"
                ) from exc
            if cur.rowcount == 0:
                raise RevisionConflictError(
                    f"observation {obs_id} was revised concurrently"
                    f" (expected revision {expected_revision}); fetch the latest and retry"
                )
            conn.execute("DELETE FROM readings WHERE observation_id = ?", (obs_id,))
            conn.executemany(
                "INSERT INTO readings (observation_id, value, is_outlier) VALUES (?, ?, ?)",
                [
                    (obs_id, value, 1 if abs(value - group_median) > tolerance else 0)
                    for value in readings
                ],
            )

            _rebuild_intervals_for_revision(conn, obs_id, old_ts, ts)
            row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            return _observation_dict(conn, row)


def _rebuild_intervals_for_revision(
    conn: sqlite3.Connection, obs_id: int, old_ts: float, new_ts: float
) -> None:
    """重建修订波及的插值区间。

    观测已更新为 new_ts（bias 也可能已变）。受影响的是两类区间：
    - 原位置相邻区间（端点时间为 old_ts）：删除后把被腾出的缺口重新接通；
    - 新位置覆盖区间（严格跨过 new_ts）：删除后由该观测在新位置一分为二。
    其余区间保持原样。所有操作在同一事务内完成。
    """
    # 记录原位置的左右邻居（修订后观测已不在 old_ts，邻居只能是其它观测）
    old_prev = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts < ? AND id <> ? ORDER BY ts DESC LIMIT 1",
        (old_ts, obs_id),
    ).fetchone()
    old_next = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts > ? AND id <> ? ORDER BY ts ASC LIMIT 1",
        (old_ts, obs_id),
    ).fetchone()

    conn.execute("DELETE FROM intervals WHERE obs_start_id = ? OR obs_end_id = ?", (obs_id, obs_id))
    conn.execute("DELETE FROM intervals WHERE t_start < ? AND t_end > ?", (new_ts, new_ts))

    # 原位置：仅当两邻居之间没有其它观测落入时，才补回接通区间
    # （此时观测行已位于新时点；新时点仍落在缺口内时，缺口将由新位置拆分覆盖，
    #  时间戳未变或小幅移动时同样如此，均不可重建）
    if old_prev and old_next and not conn.execute(
        "SELECT 1 FROM observations WHERE ts > ? AND ts < ? LIMIT 1",
        (old_prev["ts"], old_next["ts"]),
    ).fetchone():
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                old_prev["ts"], old_next["ts"], old_prev["bias"], old_next["bias"],
                old_prev["id"], old_next["id"],
            ),
        )

    # 新位置：以修订后的观测为端点拆分覆盖区间
    cur = conn.execute("SELECT id, ts, bias FROM observations WHERE id = ?", (obs_id,)).fetchone()
    _split_gap_with(conn, cur)


def list_observations() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM observations ORDER BY ts").fetchall()
        return [_observation_dict(conn, row) for row in rows]


def list_revisions(obs_id: int) -> list[dict]:
    """按版本倒序返回某观测每次成功修订前保存的快照；观测不存在抛 404。"""
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM observations WHERE id = ?", (obs_id,)).fetchone():
            raise ObservationNotFoundError(f"no observation with id {obs_id}")
        rows = conn.execute(
            "SELECT * FROM revisions WHERE observation_id = ? ORDER BY revision DESC", (obs_id,)
        ).fetchall()
        return [_revision_dict(row) for row in rows]


def _revision_dict(row: sqlite3.Row) -> dict:
    old_ts = row["old_ts"]
    return {
        "revision": row["revision"],
        "changed_at": row["changed_at"],
        "changed_at_iso": epoch_to_iso(row["changed_at"]),
        "old_timestamp": old_ts,
        "old_timestamp_iso": epoch_to_iso(old_ts),
        "old_reference": row["old_reference"],
        "old_tolerance": row["old_tolerance"],
        "old_bias": row["old_bias"],
        "old_readings": json.loads(row["old_readings"]),
    }


def _observation_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    readings = conn.execute(
        "SELECT value, is_outlier FROM readings WHERE observation_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "revision": row["revision"],
        "timestamp": row["ts"],
        "timestamp_iso": epoch_to_iso(row["ts"]),
        "reference": row["reference"],
        "tolerance": row["tolerance"],
        "group_median": row["group_median"],
        "bias": row["bias"],
        "readings": [{"value": r["value"], "is_outlier": bool(r["is_outlier"])} for r in readings],
        "outliers": [r["value"] for r in readings if r["is_outlier"]],
    }


# ---------- 区间 ----------

def list_intervals() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM intervals ORDER BY t_start").fetchall()
        return [_interval_dict(row) for row in rows]


def _interval_dict(row: sqlite3.Row) -> dict:
    return {
        "t_start": row["t_start"],
        "t_end": row["t_end"],
        "start_iso": epoch_to_iso(row["t_start"]),
        "end_iso": epoch_to_iso(row["t_end"]),
        "bias_start": row["bias_start"],
        "bias_end": row["bias_end"],
    }


# ---------- 漂移补偿 ----------

def correct(ts: float, reading: float) -> dict:
    """对待校正读数施加所在时点的插值偏差，返回校正结果与区间、异常明细。"""
    with closing(connect()) as conn:
        exact = conn.execute("SELECT * FROM observations WHERE ts = ?", (ts,)).fetchone()
        if exact is not None:
            # 恰落在观测点上：直接使用该点偏差，无需插值
            bias = exact["bias"]
            interval = None
            endpoints = [exact]
        else:
            row = conn.execute(
                "SELECT * FROM intervals WHERE t_start <= ? AND t_end >= ?", (ts, ts)
            ).fetchone()
            if row is None:
                bounds = conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM observations").fetchone()
                raise OutOfCoverageError(ts, bounds["lo"], bounds["hi"])
            bias = calibration.interpolate(ts, row["t_start"], row["bias_start"], row["t_end"], row["bias_end"])
            interval = _interval_dict(row)
            endpoints = [
                conn.execute("SELECT * FROM observations WHERE id = ?", (row["obs_start_id"],)).fetchone(),
                conn.execute("SELECT * FROM observations WHERE id = ?", (row["obs_end_id"],)).fetchone(),
            ]
        return {
            "timestamp": ts,
            "timestamp_iso": epoch_to_iso(ts),
            "reading": reading,
            "bias": bias,
            "corrected": reading + bias,
            "interval": interval,
            "endpoints": [_endpoint_dict(conn, ob) for ob in endpoints],
        }


def _endpoint_dict(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict:
    outliers = conn.execute(
        "SELECT value FROM readings WHERE observation_id = ? AND is_outlier = 1 ORDER BY id",
        (obs["id"],),
    ).fetchall()
    return {
        "observation_id": obs["id"],
        "timestamp": obs["ts"],
        "timestamp_iso": epoch_to_iso(obs["ts"]),
        "bias": obs["bias"],
        "outliers": [r["value"] for r in outliers],
    }
