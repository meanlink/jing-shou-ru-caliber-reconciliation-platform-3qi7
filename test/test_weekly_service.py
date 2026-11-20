"""周报服务集成测试：用内存假对象替代 SQLAlchemy，验证发布编排。

覆盖：
- 首次发布写入披露快照与下载文件名
- 同受众并发：后到者 held，重审后按新台账遮挡
- 中断恢复：同窗口重试复用记录、attempts+1、不新增披露
- blocked / 非 published 不可下载
"""
import os
import sys
import tempfile
import unittest

# 兼容两种测试落位：仓库内 /workspace/test，或规范的 test-artifacts/gb-1018/tests
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(_HERE, "..", "backend"),
    os.path.join(_HERE, "..", "..", "..", "workspace", "backend"),
):
    if os.path.isdir(os.path.join(_candidate, "src")):
        sys.path.insert(0, os.path.abspath(_candidate))
        break

# 下载目录指向临时路径，必须在 import 服务模块前设置
_TMP = tempfile.mkdtemp(prefix="weekly-artifacts-")
os.environ["ARTIFACT_DIR"] = _TMP

# 环境无 FastAPI/SQLAlchemy 时注入最小桩，仅满足服务模块导入
import types  # noqa: E402

if "fastapi" not in sys.modules:
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=400, detail=""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi
if "sqlalchemy" not in sys.modules:
    sqlalchemy = types.ModuleType("sqlalchemy")

    class _Criterion:
        def __init__(self, left, op, right):
            self.left = types.SimpleNamespace(name=left)
            class _Op:
                def __init__(self, name): self.__name__ = name
            self.operator = _Op(op)
            self.right = types.SimpleNamespace(value=right)

    class _Column:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return _Criterion(self.name, "eq", other)

        def __le__(self, other):
            return _Criterion(self.name, "le", other)

        def in_(self, other):
            return _Criterion(self.name, "in_op", list(other))

        def desc(self):
            return self

        def asc(self):
            return self

    class _Select:
        def __init__(self, entity):
            self.column_descriptions = [{"entity": entity}]
            self._where_criteria = None
            self._order_by_clauses = None

        def where(self, *criteria):
            self._where_criteria = types.SimpleNamespace(args=list(criteria))
            return self

        def order_by(self, *clauses):
            self._order_by_clauses = clauses
            return self

        def limit(self, n):
            return self

    sqlalchemy.select = _Select
    for _name in ("String", "JSON", "Double", "ForeignKey", "Boolean", "Text", "Integer"):
        setattr(sqlalchemy, _name, type(_name, (), {"__init__": lambda self, *a, **k: None}))
    sys.modules["sqlalchemy"] = sqlalchemy
    orm = types.ModuleType("sqlalchemy.orm")

    class _Mapped:
        def __class_getitem__(cls, item):
            return cls

    orm.Mapped = _Mapped
    orm.mapped_column = lambda *a, **k: None
    orm.relationship = lambda *a, **k: None
    sys.modules["sqlalchemy.orm"] = orm
    database = types.ModuleType("src.config.database")

    class _Base:
        pass

    database.Base = _Base
    database.engine = None
    database.Session = None
    database.get_db = None
    sys.modules["src.config.database"] = database
    security = types.ModuleType("src.utils.security")
    security.hash_password = lambda x: x
    security.decode_token = lambda x: x
    sys.modules["src.utils.security"] = security

from src.services import weekly  # noqa: E402
from src.services.disclosure import MASK_VALUE  # noqa: E402


class _Col:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    def __le__(self, other):
        return ("le", self.name, other)

    def in_(self, values):
        return ("in_op", self.name, list(values))


class _FakeModel:
    __tablename__ = "weekly_deliveries"
    # 类属性是列描述符（仅用于构造 where 条件）；实例赋值同名属性会遮蔽它，互不影响
    id = _Col("id")
    weekly_report_id = _Col("weekly_report_id")
    audience = _Col("audience")
    idempotency_key = _Col("idempotency_key")
    status = _Col("status")

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.__dict__.setdefault("id", None)
        self.__dict__.setdefault("filename", "")
        self.__dict__.setdefault("checksum", "")
        self.__dict__.setdefault("attempts", 0)
        self.__dict__.setdefault("released_snapshot", {})
        self.__dict__.setdefault("decision", {})


class _FakeDatasetModel:
    __tablename__ = "datasets"


# 服务模块直接引用了 ORM 类，替换为带描述符列的假模型，供 where(...) 构造条件
weekly.WeeklyDelivery = _FakeModel
weekly.Dataset = _FakeDatasetModel


class FakeDB:
    def __init__(self):
        self._by_id = {}
        self._query_rows = []

    def get(self, model, pk):
        return self._by_id.get((model.__tablename__, pk))

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            seq = sum(1 for k in self._by_id if k[0] == obj.__tablename__) + 1
            obj.id = seq
        self._by_id[(obj.__tablename__, obj.id)] = obj

    def flush(self):
        pass

    def commit(self):
        pass

    def scalar(self, stmt):
        res = self._execute(stmt)
        return res[0] if res else None

    def scalars(self, stmt):
        return _Scalar(self._execute(stmt))

    def _execute(self, stmt):
        # 仅支持本服务用到的简单等值/IN 查询；条件为 (op, column, value) 元组
        model = stmt.column_descriptions[0]["entity"]
        rows = [v for (t, _), v in self._by_id.items() if t == model.__tablename__]
        criteria = stmt._where_criteria.args if stmt._where_criteria else []
        for op, column, value in criteria:
            if op == "eq":
                rows = [r for r in rows if getattr(r, column) == value]
            elif op == "in_op":
                rows = [r for r in rows if getattr(r, column) in value]
            elif op == "le":
                rows = [r for r in rows if getattr(r, column) <= value]
            else:
                raise AssertionError(f"unsupported operator {op}")
        if getattr(stmt, "_order_by_clauses", None):
            rows.sort(key=lambda r: r.id, reverse=True)
        return rows


class _Scalar:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeDataset:
    __tablename__ = "datasets"
    id = 1

    def __init__(self, records):
        self.records = records


class FakeReport:
    __tablename__ = "weekly_reports"

    def __init__(self, rid, audience, interval, filters, group_by="channel_day"):
        self.id = rid
        self.name = f"周报{rid}"
        self.dataset_id = 1
        self.audience = audience
        self.interval_seconds = interval
        self.filters = filters
        self.group_by = group_by
        self.enabled = True
        self.next_run = 0.0
        self.status = "queued"
        self.last_decision = {}
        self.created_by = 1
        self.created_at = 0.0


def records():
    return [
        {"day": "2026-09-01", "channel": "X", "gross": 1100.0, "refunds": 50.0, "discount": 50.0, "visits": 500},
        {"day": "2026-09-02", "channel": "X", "gross": 2200.0, "refunds": 100.0, "discount": 100.0, "visits": 40},
        {"day": "2026-09-01", "channel": "Y", "gross": 900.0, "refunds": 0.0, "discount": 0.0, "visits": 700},
    ]


class WeeklyPublishTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.add(FakeDataset(records()))
        self.interval = 604800
        self.base = {"channels": [], "date_from": None, "date_to": None}

    def test_first_publish_creates_artifact_and_snapshot(self):
        r = FakeReport(1, "G", self.interval, dict(self.base))
        d = weekly.publish_due(self.db, r, now=1_000_000.0)
        self.assertEqual(d.status, "published")
        self.assertTrue(d.filename.endswith(".csv"))
        self.assertEqual(len(d.checksum), 64)
        self.assertTrue(d.released_snapshot["visible_atoms"])
        path = weekly.artifact_path(d)
        self.assertTrue(path and os.path.exists(path))
        with open(path, "rb") as fh:
            content = fh.read().decode("utf-8")
        self.assertIn(MASK_VALUE, content)  # 低访问渠道日在下载包里是遮住的

    def test_retry_same_window_is_idempotent(self):
        r = FakeReport(1, "G", self.interval, dict(self.base))
        first = weekly.publish_due(self.db, r, now=1_000_000.0)
        retry = weekly.publish_due(self.db, r, now=1_000_010.0)  # 同一 slot
        self.assertEqual(first.id, retry.id)
        self.assertEqual(retry.attempts, 2)
        self.assertEqual(first.filename, retry.filename)
        self.assertEqual(first.released_snapshot, retry.released_snapshot)
        deliveries = [v for (t, _), v in self.db._by_id.items() if t == "weekly_deliveries"]
        self.assertEqual(len(deliveries), 1)

    def test_concurrent_second_report_held_when_union_leaks_protected_atom(self):
        # 渠道 X：day1 高访问（可披露）、day2 低访问（受保护）；Y 为无关高访问渠道。
        # A 只发 day1（筛选），B 发 X 渠道两天的聚合行——B 单独看安全，
        # 但 A∪B 可差值反推出受保护的 day2，故 B 必须 held。
        r_a = FakeReport(1, "G", self.interval,
                         {"channels": ["X"], "date_from": "2026-09-01", "date_to": "2026-09-01"},
                         group_by="channel_day")
        r_b = FakeReport(2, "G", self.interval,
                         {"channels": ["X"], "date_from": None, "date_to": None},
                         group_by="channel")
        d_a = weekly.publish_due(self.db, r_a, now=1_000_000.0)
        self.assertEqual(d_a.status, "published")
        d_b = weekly.publish_due(self.db, r_b, now=1_000_000.0)
        self.assertEqual(d_b.status, "held")
        self.assertFalse(d_b.filename)  # held 没有可下载文件
        self.assertIn("X|2026-09-02", d_b.decision["decision_reason"])

        # 下一窗口恢复：B 按含 A 的台账重审，聚合行被二次遮挡，仍保护 day2，不新增泄露
        recheck = weekly.publish_due(self.db, r_b, now=1_000_000.0 + self.interval)
        self.assertEqual(recheck.id, d_b.id)  # 同一条记录恢复，不新增披露
        self.assertTrue(any(x["suppressed"] for x in recheck.decision["rows"]))

    def test_concurrent_no_leak_both_publish(self):
        # 两份同受众周报覆盖互不重叠、且不包含低访问原子可推断路径 -> 都应正常发布
        r_a = FakeReport(1, "G", self.interval,
                         {"channels": ["Y"], "date_from": None, "date_to": None})
        r_b = FakeReport(2, "G", self.interval,
                         {"channels": ["X"], "date_from": "2026-09-01", "date_to": "2026-09-01"})
        d_a = weekly.publish_due(self.db, r_a, now=1_000_000.0)
        d_b = weekly.publish_due(self.db, r_b, now=1_000_000.0)
        self.assertEqual(d_a.status, "published")
        self.assertEqual(d_b.status, "published")  # 合起来不泄露，不得误挂起
        self.assertTrue(weekly.artifact_path(d_b))

    def test_blocked_report_has_no_download(self):
        # 只选低访问渠道日 -> 全部遮挡 -> blocked
        r = FakeReport(1, "G", self.interval,
                       {"channels": ["X"], "date_from": "2026-09-02", "date_to": "2026-09-02"})
        d = weekly.publish_due(self.db, r, now=1_000_000.0)
        self.assertEqual(d.status, "blocked")
        self.assertIsNone(weekly.artifact_path(d))

    def test_different_audiences_do_not_block_each_other(self):
        r_a = FakeReport(1, "G1", self.interval, dict(self.base))
        r_b = FakeReport(2, "G2", self.interval, dict(self.base))
        d_a = weekly.publish_due(self.db, r_a, now=1_000_000.0)
        d_b = weekly.publish_due(self.db, r_b, now=1_000_000.0)
        self.assertEqual(d_a.status, "published")
        self.assertEqual(d_b.status, "published")  # 不同受众不构成并发冲突


if __name__ == "__main__":
    unittest.main(verbosity=2)
