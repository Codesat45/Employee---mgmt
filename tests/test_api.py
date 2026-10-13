import os
import random
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["MONGO_URI"] = os.environ.get("MONGO_TEST_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGO_DB"] = f"attendance_api_tests_{os.getpid()}_{random.randrange(100000, 1000000)}"

from fastapi.testclient import TestClient

from app.main import (
    _attendance_date_for_shift,
    _compute_late_minutes,
    _compute_overtime_minutes,
    _compute_work_hours,
    _truncate_epoch_ms,
    app,
    client as mongo_client,
    db,
)
from sample_seed import seed_database


IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def epoch_ms(local_iso: str) -> int:
    return int(datetime.fromisoformat(local_iso).replace(tzinfo=IST).timestamp() * 1000)


class AttendanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            db.command("ping")
        except Exception as exc:
            raise RuntimeError(
                "MongoDB is required for integration tests. Start MongoDB or set MONGO_TEST_URI."
            ) from exc

    @classmethod
    def tearDownClass(cls) -> None:
        mongo_client.drop_database(db.name)
        mongo_client.close()

    def setUp(self) -> None:
        self.api = TestClient(app)
        self.api.__enter__()
        self.emp_codes: set[str] = set()

    def tearDown(self) -> None:
        if self.emp_codes:
            db.attendance_logs.delete_many({"emp_code": {"$in": list(self.emp_codes)}})
            db.employees.delete_many({"emp_code": {"$in": list(self.emp_codes)}})
        self.api.__exit__(None, None, None)

    def new_emp_code(self) -> str:
        while True:
            code = f"EMP{random.randrange(100000, 1000000)}"
            if code not in self.emp_codes and db.employees.find_one({"emp_code": code}) is None:
                self.emp_codes.add(code)
                return code

    def employee_payload(self, code: str, **overrides: str) -> dict[str, str]:
        return {
            "emp_code": code,
            "name": "Test Employee",
            "email": f"{code.lower()}@example.test",
            "department": "Test Department",
            "joined_on": "2026-10-01",
            **overrides,
        }

    def create_employee(self, **overrides: str) -> str:
        code = self.new_emp_code()
        response = self.api.post("/employees", json=self.employee_payload(code, **overrides))
        self.assertEqual(response.status_code, 201, response.text)
        return code

    def test_health_and_contract_operation_count(self) -> None:
        self.assertEqual(self.api.get("/health").status_code, 200)
        contract = self.api.get("/openapi.json")
        self.assertEqual(contract.status_code, 200)
        operation_count = sum(len(methods) for methods in contract.json()["paths"].values())
        self.assertEqual(operation_count, 12)

    def insert_log(
        self,
        emp_code: str,
        attendance_date: str,
        status: str,
        *,
        late_minutes: int = 0,
        overtime_minutes: int = 0,
        half_day: bool = False,
        work_hours: float | None = None,
    ) -> None:
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": attendance_date,
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
            "history": [],
        })

    def test_employee_validation_listing_and_create_race(self) -> None:
        code = self.new_emp_code()
        payload = self.employee_payload(
            code,
            name="N" * 100,
            email="a" * 107 + "@example.test",
            department="D" * 50,
        )
        created = self.api.post("/employees", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertIsInstance(created.json()["created_at"], int)

        for field, value in (
            ("name", "N" * 101),
            ("email", "a" * 108 + "@example.test"),
            ("department", "D" * 51),
            ("name", "   "),
        ):
            invalid = self.employee_payload(self.new_emp_code(), **{field: value})
            self.assertEqual(self.api.post("/employees", json=invalid).status_code, 422, field)

        listing = self.api.get("/employees", params={
            "department": "D" * 50,
            "page": 1,
            "page_size": 1,
        })
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 1)
        self.assertEqual(len(listing.json()["items"]), 1)
        self.assertEqual(self.api.get("/employees", params={"page_size": 101}).status_code, 422)

        race_code = self.new_emp_code()
        race_payload = self.employee_payload(race_code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda _: self.api.post("/employees", json=race_payload),
                range(2),
            ))
        self.assertCountEqual([response.status_code for response in responses], [201, 409])

    def test_time_rule_boundaries_and_timestamp_validation(self) -> None:
        self.assertEqual(
            _attendance_date_for_shift(epoch_ms("2026-10-14T02:00:00"), "22:00", "06:00"),
            "2026-10-13",
        )
        self.assertEqual(_compute_late_minutes(epoch_ms("2026-10-13T09:40:00"), "09:30", "18:30"), 0)
        self.assertEqual(_compute_late_minutes(epoch_ms("2026-10-13T09:40:01"), "09:30", "18:30"), 10)
        self.assertEqual(_compute_late_minutes(epoch_ms("2026-10-13T10:15:59"), "09:30", "18:30"), 45)
        self.assertEqual(
            _compute_overtime_minutes(
                epoch_ms("2026-10-13T09:30:00"),
                epoch_ms("2026-10-13T18:59:59"),
                "09:30",
                "18:30",
            ),
            0,
        )
        self.assertEqual(
            _compute_overtime_minutes(
                epoch_ms("2026-10-13T09:30:00"),
                epoch_ms("2026-10-13T19:00:00"),
                "09:30",
                "18:30",
            ),
            30,
        )
        self.assertEqual(
            _compute_work_hours(epoch_ms("2026-10-13T09:30:00"), epoch_ms("2026-10-13T13:59:59")),
            4.5,
        )
        self.assertEqual(_truncate_epoch_ms(epoch_ms("2026-10-13T09:40:00") + 900), epoch_ms("2026-10-13T09:40:00"))

        code = self.create_employee()
        for bad_timestamp in (1_700_000_000, 1_700_000_000.0, "1791870000000", 99_999_999_999):
            response = self.api.post("/attendance/punch-in", json={
                "emp_code": code,
                "punched_at": bad_timestamp,
            })
            self.assertEqual(response.status_code, 422, repr(bad_timestamp))

    def test_punch_in_and_punch_out_are_race_safe(self) -> None:
        code = self.create_employee()
        punch_in_body = {
            "emp_code": code,
            "punched_at": epoch_ms("2026-10-13T09:40:01"),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda _: self.api.post("/attendance/punch-in", json=punch_in_body),
                range(2),
            ))
        self.assertCountEqual([response.status_code for response in responses], [201, 409])
        created = next(response.json() for response in responses if response.status_code == 201)
        self.assertEqual(created["date"], "2026-10-13")
        self.assertEqual(created["late_minutes"], 10)

        punch_out_body = {
            "emp_code": code,
            "punched_at": epoch_ms("2026-10-13T19:00:00"),
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda _: self.api.post("/attendance/punch-out", json=punch_out_body),
                range(2),
            ))
        self.assertCountEqual([response.status_code for response in responses], [200, 409])
        closed = next(response.json() for response in responses if response.status_code == 200)
        self.assertEqual(closed["work_hours"], 9.33)
        self.assertEqual(closed["overtime_minutes"], 30)

        short_code = self.create_employee()
        self.assertEqual(self.api.post("/attendance/punch-in", json={
            "emp_code": short_code,
            "punched_at": epoch_ms("2026-10-13T09:30:00"),
        }).status_code, 201)
        short_day = self.api.post("/attendance/punch-out", json={
            "emp_code": short_code,
            "punched_at": epoch_ms("2026-10-13T13:59:00"),
        })
        self.assertEqual(short_day.status_code, 200, short_day.text)
        self.assertEqual(short_day.json()["work_hours"], 4.48)
        self.assertTrue(short_day.json()["half_day"])

        boundary_code = self.create_employee()
        self.assertEqual(self.api.post("/attendance/punch-in", json={
            "emp_code": boundary_code,
            "punched_at": epoch_ms("2026-10-13T09:30:00"),
        }).status_code, 201)
        boundary_day = self.api.post("/attendance/punch-out", json={
            "emp_code": boundary_code,
            "punched_at": epoch_ms("2026-10-13T13:59:59"),
        })
        self.assertEqual(boundary_day.status_code, 200, boundary_day.text)
        self.assertEqual(boundary_day.json()["work_hours"], 4.5)
        self.assertFalse(boundary_day.json()["half_day"])

    def test_listing_regularization_and_concurrent_history(self) -> None:
        code = self.create_employee()
        punch_in = self.api.post("/attendance/punch-in", json={
            "emp_code": code,
            "punched_at": epoch_ms("2026-10-13T09:30:00"),
        })
        self.assertEqual(punch_in.status_code, 201, punch_in.text)

        attendance = self.api.get("/attendance", params={
            "emp_code": code,
            "date_from": "2026-10-13",
            "date_to": "2026-10-13",
            "status": "PRESENT",
            "page": 1,
            "page_size": 1,
        })
        self.assertEqual(attendance.status_code, 200, attendance.text)
        self.assertEqual(attendance.json()["total"], 1)
        self.assertEqual(self.api.get("/attendance", params={
            "date_from": "2026-02-30",
        }).status_code, 422)
        self.assertEqual(self.api.get("/attendance", params={
            "date_from": "2026-10-14",
            "date_to": "2026-10-13",
        }).status_code, 422)

        record_url = f"/attendance/{code}/2026-10-13"
        corrections = [
            {
                "status": "WFH",
                "reason": "concurrent status correction",
                "regularized_by": "test-a",
            },
            {
                "punch_in": epoch_ms("2026-10-13T10:00:00"),
                "reason": "concurrent time correction",
                "regularized_by": "test-b",
            },
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda body: self.api.patch(record_url, json=body),
                corrections,
            ))
        self.assertTrue(all(response.status_code in (200, 409) for response in responses))
        history = db.attendance_logs.find_one({"emp_code": code, "date": "2026-10-13"})["history"]
        successful_updates = sum(response.status_code == 200 for response in responses)
        self.assertEqual(len(history), successful_updates)
        self.assertGreaterEqual(successful_updates, 1)
        self.assertEqual(self.api.patch(
            f"/attendance/{code}/2026-10-12",
            json={
                "status": "WFH",
                "reason": "missing record",
                "regularized_by": "test",
            },
        ).status_code, 404)

    def test_analytics_ties_headcount_gaps_and_explain_routes(self) -> None:
        department = f"Test-{self.new_emp_code()}"
        mid_month = self.create_employee(department="MidMonth", joined_on="2026-10-07")
        mid_month_summary = self.api.get(
            f"/analytics/employees/{mid_month}/monthly",
            params={"month": "2026-10"},
        )
        self.assertEqual(mid_month_summary.status_code, 200, mid_month_summary.text)
        self.assertEqual(mid_month_summary.json()["working_days"], 18)
        self.assertEqual(mid_month_summary.json()["present_days"], 0.0)

        employee_a = self.create_employee(department=department, joined_on="2026-10-01")
        employee_b = self.create_employee(department=department, joined_on="2026-10-03")
        employee_c = self.create_employee(department=department, joined_on="2026-10-01")
        no_logs = self.create_employee(department=department, joined_on="2026-10-01")
        future = self.create_employee(department=department, joined_on="2026-11-01")
        future_summary = self.api.get(
            f"/analytics/employees/{future}/monthly",
            params={"month": "2026-10"},
        )
        self.assertEqual(future_summary.status_code, 200, future_summary.text)
        self.assertEqual(future_summary.json()["working_days"], 0)
        self.assertIsNone(future_summary.json()["attendance_pct"])

        self.insert_log(
            employee_a, "2026-10-05", "PRESENT",
            late_minutes=60, overtime_minutes=30, half_day=True, work_hours=4.5,
        )
        self.insert_log(employee_a, "2026-10-04", "PRESENT", late_minutes=15, work_hours=8.0)
        self.insert_log(employee_b, "2026-10-05", "WFH", late_minutes=30, work_hours=8.0)
        self.insert_log(employee_c, "2026-10-05", "ON_DUTY", late_minutes=30, work_hours=8.0)
        self.insert_log(employee_c, "2026-10-02", "LEAVE")

        monthly = self.api.get(
            f"/analytics/employees/{employee_a}/monthly",
            params={"month": "2026-10"},
        )
        self.assertEqual(monthly.status_code, 200, monthly.text)
        self.assertEqual(monthly.json()["working_days"], 22)
        self.assertEqual(monthly.json()["present_days"], 0.5)
        self.assertEqual(monthly.json()["total_late_minutes"], 75)

        summary = self.api.get("/analytics/departments/summary", params={
            "month": "2026-10",
            "department": department,
        })
        self.assertEqual(summary.status_code, 200, summary.text)
        item = summary.json()["items"][0]
        self.assertEqual(item["headcount"], 4)
        self.assertEqual(item["present_days"], 2.5)
        self.assertEqual(item["avg_work_hours"], 7.13)
        self.assertEqual(item["leave_count"], 1)
        self.assertEqual(len(summary.json()["items"]), 1)

        leaderboard = self.api.get("/analytics/leaderboard/late", params={
            "month": "2026-10",
            "department": department,
            "limit": 2,
        })
        self.assertEqual(leaderboard.status_code, 200, leaderboard.text)
        rows = leaderboard.json()["items"]
        self.assertEqual([row["rank"] for row in rows], [1, 2, 2])
        self.assertEqual({row["emp_code"] for row in rows[1:]}, {employee_b, employee_c})

        trend = self.api.get(
            f"/analytics/departments/{department}/trend",
            params={"from": "2026-10-03", "to": "2026-10-07"},
        )
        self.assertEqual(trend.status_code, 200, trend.text)
        rows = trend.json()["items"]
        self.assertEqual([row["date"] for row in rows], [
            "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
        ])
        self.assertEqual(rows[0]["headcount"], 4)
        self.assertEqual(rows[0]["headcount"], len({employee_a, employee_b, employee_c, no_logs}))
        self.assertFalse(rows[1]["is_working_day"])
        self.assertIsNone(rows[1]["attendance_rate"])
        self.assertEqual(rows[2]["attendance_rate"], 0.625)
        self.assertEqual(rows[3]["attendance_rate"], 0.0)
        self.assertEqual(rows[4]["moving_avg_7d"], 0.2083)

        explain_requests = {
            "attendance_list": {"emp_code": employee_a, "page": 1, "page_size": 10},
            "employee_monthly": {"emp_code": employee_a, "month": "2026-10"},
            "department_summary": {"month": "2026-10", "department": department},
            "late_leaderboard": {"month": "2026-10", "department": department, "limit": 2},
            "department_trend": {"department": department, "from": "2026-10-03", "to": "2026-10-07"},
        }
        for endpoint, params in explain_requests.items():
            with self.subTest(explain_endpoint=endpoint):
                response = self.api.get(f"/admin/explain/{endpoint}", params=params)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["endpoint"], endpoint)
                self.assertIn("executionStats", str(payload["explain"]))

        self.assertEqual(self.api.get(
            "/admin/explain/employee_monthly",
            params={"emp_code": employee_a},
        ).status_code, 422)
        self.assertEqual(self.api.get("/admin/explain/not_an_endpoint").status_code, 404)
        self.assertEqual(self.api.get(
            f"/analytics/departments/{department}/trend",
            params={"from": "2026-10-01", "to": "2027-01-01"},
        ).status_code, 422)

    def test_sample_seed_requires_destructive_confirmation(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "sample_seed.py")],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--confirm-destructive", result.stderr)

    def test_sample_seed_validates_all_fixtures_before_deleting_data(self) -> None:
        marker = self.new_emp_code()
        db.employees.insert_one({
            **self.employee_payload(marker),
            "created_at": datetime.now(timezone.utc),
        })
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                seed_database(db, Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            fixture_directory = Path(directory)
            (fixture_directory / "employees.json").write_text("[]", encoding="utf-8")
            (fixture_directory / "attendance_logs.json").write_text("[", encoding="utf-8")
            with self.assertRaises(ValueError):
                seed_database(db, fixture_directory)
        self.assertIsNotNone(db.employees.find_one({"emp_code": marker}))


if __name__ == "__main__":
    unittest.main()
