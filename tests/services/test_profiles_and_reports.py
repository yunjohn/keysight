from pathlib import Path

from keysight_scope_app.services.reporting import ReportExporter
from keysight_scope_app.services.test_profiles import (
    MetricLimit,
    MetricStatus,
    TestProfile,
    TestProfileRepository,
    TestRun,
    evaluate_metric,
)


def test_metric_boundary_and_inconclusive_status() -> None:
    limit = MetricLimit("启动时间", "s", target=0.2, minimum=0.1, maximum=0.3)
    assert evaluate_metric(limit, 0.1).status is MetricStatus.PASS
    assert evaluate_metric(limit, 0.31).status is MetricStatus.FAIL
    assert evaluate_metric(limit, None).status is MetricStatus.INCONCLUSIVE


def test_profile_round_trip_and_duplicate(tmp_path: Path) -> None:
    repository = TestProfileRepository(tmp_path)
    profile = TestProfile(
        name="标准方案",
        profile_version="1.0",
        channel_roles={"control": "CHANnel1"},
        capture={"points": 1000},
        analysis={"mode": "full"},
        metric_limits=(MetricLimit("启动时间", "s", maximum=0.3),),
    )
    loaded = repository.load(repository.save(profile))
    duplicate = repository.duplicate(loaded, "标准方案副本")
    assert loaded == profile
    assert duplicate.name == "标准方案副本"
    assert repository.list_profiles()


def test_run_status_prefers_fail_then_inconclusive() -> None:
    passing = evaluate_metric(MetricLimit("A", maximum=2), 1)
    unknown = evaluate_metric(MetricLimit("B"), None)
    failing = evaluate_metric(MetricLimit("C", maximum=1), 2)
    assert TestRun("S1", "P", "1", (passing, unknown)).status is MetricStatus.INCONCLUSIVE
    assert TestRun("S1", "P", "1", (passing, failing)).status is MetricStatus.FAIL


def test_report_contains_traceability_fields(tmp_path: Path) -> None:
    run = TestRun(
        sample_id="MOTOR-001",
        profile_name="Brake",
        profile_version="2",
        instrument_id="KEYSIGHT,DSOX",
        waveform_path="raw.csv",
        metrics=(evaluate_metric(MetricLimit("刹车时间", "s", maximum=0.2), 0.1),),
    )
    exporter = ReportExporter()
    html = exporter.export_html(run, tmp_path / "report.html").read_text(encoding="utf-8")
    exporter.export_csv([run], tmp_path / "report.csv")
    assert run.run_id in html
    assert "MOTOR-001" in html
    assert "raw.csv" in html
    assert (tmp_path / "report.csv").read_text(encoding="utf-8-sig").startswith("run_id,")

